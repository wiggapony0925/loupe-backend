"""Native push notifications via Expo's push API.

Mirrors the email transport's philosophy: best-effort, never raises, and
self-cleaning — Expo's ``DeviceNotRegistered`` receipt prunes dead tokens
(uninstalled app, rotated token) so the registry never rots. Sends respect
``UserSettings.push_notifications_enabled``.

No credentials needed: Expo's push service authenticates the app by the
token itself (they're scoped to the Expo project).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.db import get_sessionmaker
from app.models.push_token import PushToken
from app.models.user import UserSettings
from app.utils.logger import get_logger

logger = get_logger("push")

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_CHUNK = 100  # Expo max messages per request


async def _tokens_for_user(user_id: uuid.UUID) -> list[str]:
    """The user's registered device tokens — empty when push is disabled."""
    sm = get_sessionmaker()
    async with sm() as db:
        settings_row = (
            await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
        ).scalar_one_or_none()
        if settings_row is not None and not settings_row.push_notifications_enabled:
            return []
        rows = (
            (
                await db.execute(
                    select(PushToken.token).where(PushToken.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _prune(tokens: list[str]) -> None:
    if not tokens:
        return
    try:
        sm = get_sessionmaker()
        async with sm() as db:
            await db.execute(delete(PushToken).where(PushToken.token.in_(tokens)))
            await db.commit()
            logger.info("pruned %d dead push token(s)", len(tokens))
    except Exception as exc:
        logger.warning("push token prune failed (%s)", exc)


async def send_to_user(
    user_id: uuid.UUID,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Push to every device the user has registered. Returns messages accepted."""
    tokens = await _tokens_for_user(user_id)
    if not tokens:
        return 0
    accepted = 0
    dead: list[str] = []
    for start in range(0, len(tokens), _CHUNK):
        chunk = tokens[start : start + _CHUNK]
        messages = [
            {
                "to": t,
                "title": title,
                "body": body,
                "sound": "default",
                "data": data or {},
            }
            for t in chunk
        ]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(EXPO_PUSH_URL, json=messages)
            if resp.status_code >= 400:
                logger.warning(
                    "expo push error %s: %s", resp.status_code, resp.text[:200]
                )
                continue
            tickets = (resp.json() or {}).get("data") or []
            for token, ticket in zip(chunk, tickets, strict=False):
                if ticket.get("status") == "ok":
                    accepted += 1
                elif (ticket.get("details") or {}).get(
                    "error"
                ) == "DeviceNotRegistered":
                    dead.append(token)
                else:
                    logger.info("push ticket error: %s", ticket.get("message"))
        except Exception as exc:  # never let push break the caller
            logger.warning("expo push failed (%s)", exc)
    await _prune(dead)
    return accepted


async def send_price_alert_push(
    user_id: uuid.UUID,
    *,
    card_name: str,
    condition: str,
    price_usd: float,
    threshold_usd: float,
    card_id: Any,
) -> int:
    """The price-alert push — the phone-native leg of alert → email → bell."""
    arrow = "▲" if condition == "above" else "▼"
    direction = "climbed above" if condition == "above" else "dropped below"
    return await send_to_user(
        user_id,
        title=f"{arrow} {card_name} — ${float(price_usd):,.2f}",
        body=f"Just {direction} your ${float(threshold_usd):,.2f} alert.",
        data={"type": "price_alert", "cardId": str(card_id)},
    )


__all__ = ["send_price_alert_push", "send_to_user"]
