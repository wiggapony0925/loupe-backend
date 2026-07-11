"""Daily price tick for OWNED cards — the portfolio chart's data supply.

The portfolio chart derives its shape from per-card
``card_metadata['price_history']`` daily points. Those points are written
write-on-read when a card's market snapshot is served — which only covers
cards users actually open. Vault holdings nobody taps never accrue points,
so ``_value_on`` falls back to a flat ratio and the chart renders a
straight line **because the data doesn't exist**, not because the market
is calm (prod measurement: median owned card had exactly ONE point).

This module closes that gap without the offline worker: whenever a user
loads their portfolio history (the chart), we opportunistically refresh
the stalest owned cards in the background —

* at most once per user per ``_THROTTLE_TTL`` (kv-cache key),
* at most ``DEFAULT_LIMIT`` cards per sweep (quota-friendly; a whole
  vault converges within a few chart loads),
* via ``get_card(force_refresh=True)`` (upstream re-resolve + persisted
  ``pricing_summary``) + ``record_price_observation`` (today's point,
  idempotent per UTC day).

Best effort by design: any failure is swallowed — the chart read that
triggered the sweep must never break.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.db import get_sessionmaker
from app.models.card import Card
from app.models.grade import GradedCard
from app.tasks.price_snapshot import record_price_observation
from app.utils.logger import get_logger

logger = get_logger("workers.price_freshness")

#: Max cards refreshed per sweep — bounds upstream fan-out per user.
DEFAULT_LIMIT = 30

#: Min interval between sweeps for one user.
_THROTTLE_TTL = 6 * 60 * 60  # 6 hours

_THROTTLE_PREFIX = "loupe:pricefresh"

#: Keep strong references to fire-and-forget sweeps so they aren't GC'd.
_running: set[asyncio.Task] = set()


def _amount_from_pricing(pricing: Any) -> float | None:
    """Raw market amount out of a ``pricing_summary`` dict, else None."""
    if not isinstance(pricing, dict):
        return None
    for key in ("market", "raw"):
        block = pricing.get(key)
        if isinstance(block, dict):
            amount = block.get("amount")
            try:
                value = float(amount)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _latest_history_date(meta: Any) -> str:
    """Latest recorded price date ("" when the card has no history)."""
    if not isinstance(meta, dict):
        return ""
    history = meta.get("price_history")
    if not isinstance(history, list):
        return ""
    latest = ""
    for entry in history:
        if isinstance(entry, dict):
            raw = entry.get("date")
            if isinstance(raw, str) and raw[:10] > latest:
                latest = raw[:10]
    return latest


async def refresh_owned_prices(
    user_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT
) -> dict[str, int]:
    """Refresh today's price for the user's stalest owned cards.

    Returns ``{"considered": n, "refreshed": k, "recorded": r}``.
    """
    today = datetime.now(UTC).date().isoformat()
    sm = get_sessionmaker()
    async with sm() as session:
        card_ids = (
            (
                await session.execute(
                    select(GradedCard.card_id)
                    .where(
                        GradedCard.user_id == user_id,
                        GradedCard.deleted_at.is_(None),
                        GradedCard.card_id.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not card_ids:
            return {"considered": 0, "refreshed": 0, "recorded": 0}
        rows = (
            (await session.execute(select(Card).where(Card.id.in_(card_ids))))
            .scalars()
            .all()
        )
        # Stalest first; cards already ticked today need nothing.
        stale = sorted(
            (c for c in rows if _latest_history_date(c.card_metadata) < today),
            key=lambda c: _latest_history_date(c.card_metadata),
        )[:limit]
        stale_ids = [str(c.id) for c in stale]

    refreshed = 0
    recorded = 0
    # Import here: card_search_service pulls the provider registry at import
    # time, which test conftest replaces after module import ordering.
    from app.services.catalog import card_search_service

    for cid in stale_ids:
        try:
            card = await card_search_service.get_card(cid, force_refresh=True)
        except Exception:  # pragma: no cover — one bad card can't stop the sweep
            logger.debug("freshness resolve failed for %s", cid, exc_info=True)
            continue
        amount = _amount_from_pricing((card or {}).get("pricing_summary"))
        if amount is None:
            continue
        refreshed += 1
        if await record_price_observation(cid, amount):
            recorded += 1

    result = {
        "considered": len(stale_ids),
        "refreshed": refreshed,
        "recorded": recorded,
    }
    if stale_ids:
        logger.info("price freshness sweep for %s: %s", user_id, result)
    return result


async def _throttled_sweep(user_id: uuid.UUID) -> None:
    """Body of the fire-and-forget sweep — throttle check + refresh."""
    try:
        from app.platform.redis_client import get_redis

        key = f"{_THROTTLE_PREFIX}:{user_id}"
        r = await get_redis()
        if await r.get(key) is not None:
            return
        await r.setex(key, _THROTTLE_TTL, datetime.now(UTC).isoformat())
        await refresh_owned_prices(user_id)
    except Exception:  # pragma: no cover — background work is always optional
        logger.warning("price freshness sweep failed for %s", user_id, exc_info=True)


def kick_owned_price_refresh(user_id: uuid.UUID) -> None:
    """Fire-and-forget a throttled freshness sweep for *user_id*.

    Called from the portfolio-history read path (the chart): the response
    returns immediately; the sweep tops up stale prices in the background
    so tomorrow's chart has today's points.
    """
    try:
        task = asyncio.get_running_loop().create_task(_throttled_sweep(user_id))
        _running.add(task)
        task.add_done_callback(_running.discard)
    except RuntimeError:  # pragma: no cover — no loop (sync test context)
        pass


__all__ = [
    "DEFAULT_LIMIT",
    "kick_owned_price_refresh",
    "refresh_owned_prices",
]
