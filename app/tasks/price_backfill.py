"""Backfill ``Card.card_metadata['pricing_summary']`` from upstream catalogs.

Locally-seeded cards (UUID ids, no ``source:id`` link) don't get pricing for
free.  This worker iterates such rows and asks
:func:`card_search_service.resolve_pricing_for_local` to look them up on
Pokémon TCG / Scryfall / YGOPRODeck — the prices those APIs already embed for
free — and persists the result so subsequent requests skip the upstream call.

Side-effect: whenever a fresh latest price lands for a card, we also
evaluate any pending :class:`~app.models.price_alert.PriceAlert` rows
for that card. The alert evaluator atomically flips matching rows and
returns the ones that just fired — callers can fan out push
notifications from the returned list.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import get_sessionmaker
from app.models.card import Card
from app.models.price import PriceSnapshot
from app.models.user import User
from app.services import email_service, push_service
from app.services.catalog import card_resolver_service, card_search_service
from app.services.collection import holding_valuation_service
from app.services.market import price_alert_service
from app.utils.logger import get_logger

logger = get_logger("workers.price_backfill")

#: Max rows to touch per run — keeps each job bounded.
DEFAULT_BATCH_SIZE = 200

#: Small gap between upstream calls so we don't hammer the public APIs.
_INTER_CALL_DELAY_SEC = 0.25


async def backfill_prices(
    ctx: dict[str, Any] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    force: bool = False,
) -> dict[str, int]:
    """Resolve and persist pricing for up to ``batch_size`` local cards.

    Returns a counter dict: ``{"scanned": n, "updated": k, "missed": m}``.
    """
    scanned = 0
    updated = 0
    missed = 0
    alerts_fired = 0
    # Alert emails to fan out after the batch commits (never before — an
    # email about an alert whose triggered_at didn't persist would re-send
    # on the next run).
    pending_notices: list[dict[str, Any]] = []

    sm = get_sessionmaker()
    async with sm() as session:
        stmt = (
            select(Card)
            .options(selectinload(Card.card_set))
            .order_by(Card.updated_at.asc())
            .limit(batch_size)
        )
        rows = (await session.execute(stmt)).scalars().all()

        for row in rows:
            scanned += 1
            meta = row.card_metadata if isinstance(row.card_metadata, dict) else {}
            if not force and meta.get("pricing_summary"):
                continue

            card_set = getattr(row, "card_set", None)
            set_code = card_set.code if card_set is not None else None
            tcg_str = row.tcg.value if hasattr(row.tcg, "value") else str(row.tcg)

            resolved = await card_search_service.resolve_pricing_for_local(
                tcg=tcg_str,
                name=row.name,
                set_code=set_code,
                number=row.number,
            )
            if resolved is None:
                missed += 1
            else:
                new_meta = dict(meta)
                pricing = resolved.get("pricing_summary")
                if pricing:
                    new_meta["pricing_summary"] = pricing
                if resolved.get("image_url"):
                    new_meta["image_url"] = resolved["image_url"]
                if resolved.get("images"):
                    new_meta["images"] = resolved["images"]
                if new_meta != meta:
                    row.card_metadata = new_meta
                    updated += 1

                # Evaluate any pending price alerts against the newly
                # observed market price. Skipped if upstream didn't
                # return a numeric market value (we won't fire on
                # missing data).
                market_obj = (
                    pricing.get("market") if isinstance(pricing, dict) else None
                )
                market_amt = (
                    market_obj.get("amount") if isinstance(market_obj, dict) else None
                )
                if market_amt is not None:
                    try:
                        latest = Decimal(str(market_amt))
                    except (ArithmeticError, ValueError):
                        latest = None
                    if latest is not None:
                        fired = await price_alert_service.evaluate_for_card(
                            session, row.id, latest
                        )
                        alerts_fired += len(fired)
                        # Snapshot everything the notification needs now —
                        # the ORM rows expire once the batch commits below.
                        for alert in fired:
                            pending_notices.append(
                                {
                                    "user_id": alert.user_id,
                                    "card_id": row.id,
                                    "card_name": row.name,
                                    "set_name": (card_set.name if card_set else None),
                                    "condition": (
                                        alert.condition.value
                                        if hasattr(alert.condition, "value")
                                        else str(alert.condition)
                                    ),
                                    "threshold_usd": alert.threshold_usd,
                                    "price_usd": latest,
                                    # Card art + recent history make the
                                    # alert email a mini card-detail page.
                                    "image_url": new_meta.get("image_url"),
                                }
                            )

                # Persist the upstream link so future requests skip name search.
                upstream_id = resolved.get("id")
                if upstream_id and ":" in upstream_id:
                    src, _, ext = upstream_id.partition(":")
                    await card_resolver_service.link_external_ref(
                        session,
                        card_id=row.id,
                        source=src,
                        external_id=ext,
                        confidence=0.9,
                    )

            await asyncio.sleep(_INTER_CALL_DELAY_SEC)

        if updated or alerts_fired:
            await session.commit()

        # Fan out "your alert fired" emails — the notification channel the
        # web client relies on (no push there). Alerts are one-shot rows the
        # user explicitly created, so this is transactional mail.
        if pending_notices:
            user_ids = {n["user_id"] for n in pending_notices}
            user_rows = (
                await session.execute(
                    select(User.id, User.email).where(User.id.in_(user_ids))
                )
            ).all()
            emails = {uid: email for uid, email in user_rows if email}
            for notice in pending_notices:
                email = emails.get(notice["user_id"])
                if not email:
                    continue
                # Recent observations power the sparkline in the email.
                snaps = (
                    (
                        await session.execute(
                            select(PriceSnapshot.price_usd)
                            .where(PriceSnapshot.card_id == notice["card_id"])
                            .order_by(PriceSnapshot.created_at.desc())
                            .limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )
                history = [float(p) for p in reversed(snaps)]
                history.append(float(notice["price_usd"]))
                await email_service.send_price_alert(
                    email,
                    card_name=notice["card_name"],
                    set_name=notice["set_name"],
                    condition=notice["condition"],
                    threshold_usd=notice["threshold_usd"],
                    price_usd=notice["price_usd"],
                    card_id=notice["card_id"],
                    image_url=notice.get("image_url"),
                    history=history if len(history) >= 2 else None,
                )
                # The phone-native leg (inbox + bell + lock screen).
                await push_service.send_price_alert_push(
                    notice["user_id"],
                    card_name=notice["card_name"],
                    condition=notice["condition"],
                    price_usd=float(notice["price_usd"]),
                    threshold_usd=float(notice["threshold_usd"]),
                    card_id=notice["card_id"],
                )

    # Sends are queued in the background; flush them before the job returns
    # so a one-shot worker process doesn't exit with mail still pending.
    await email_service.drain()

    # Holdings created before valuation-on-create — and every quick-add that
    # predates it — stored a NULL value, so those vaults read $0. Prices have
    # just been refreshed above, so this is the right moment to heal them.
    # Only ever fills NULLs; owner-set values (including 0) are untouched.
    valued = 0
    async with get_sessionmaker()() as session:
        valued = await holding_valuation_service.backfill_missing_values(session)

    result = {
        "scanned": scanned,
        "updated": updated,
        "missed": missed,
        "alerts_fired": alerts_fired,
        "holdings_valued": valued,
    }
    logger.info("price_backfill complete: %s", result)
    return result


__all__ = ["backfill_prices"]
