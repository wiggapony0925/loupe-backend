"""Snapshot each card's live ``pricing_summary.market.amount`` into
``Card.card_metadata['price_history']`` so the portfolio history
endpoint has real day-over-day prices to compute deltas against.

Without this cron, owned cards have a live market price (refreshed
by ``catalog_sync`` / ``price_backfill``) but no recorded historical
points — which forced the portfolio service into estimate-based
fallbacks and produced bogus deltas like "+284% today" against
scan-time ``GradedCard.estimated_value_usd`` values.

The job:

* streams cards in batches (default 500/run)
* reads ``card_metadata['pricing_summary']['market']['amount']``
* appends ``{"date": today, "priceUsd": amount}`` to
  ``card_metadata['price_history']`` if today's entry doesn't
  already exist (replaces it if the value drifted)
* sorts and trims history to ``MAX_HISTORY_POINTS`` rolling days

Idempotent: running twice in the same UTC day is a no-op for any
card whose recorded value hasn't changed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import get_sessionmaker
from app.models.card import Card
from app.utils.logger import get_logger

logger = get_logger("workers.price_snapshot")

#: How many cards to fetch per query page.
DEFAULT_BATCH_SIZE = 500

#: Rolling window of daily points to retain. ~2 years lets the ALL
#: range render real curves while keeping the JSONB payload small.
MAX_HISTORY_POINTS = 730


def _extract_market_amount(meta: dict | None) -> float | None:
    if not isinstance(meta, dict):
        return None
    pricing = meta.get("pricing_summary")
    if not isinstance(pricing, dict):
        return None
    market = pricing.get("market")
    if not isinstance(market, dict):
        return None
    amount = market.get("amount")
    if amount is None:
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def _upsert_today(
    history: list[dict] | None, today: date, price: float
) -> tuple[list[dict], bool]:
    """Return a new history list with today's price upserted.

    Second return value is ``True`` when the list actually changed
    (avoids writing JSONB + bumping ``updated_at`` for no-op runs).
    """
    today_iso = today.isoformat()
    out: list[dict] = []
    seen_today = False
    changed = False
    src = history if isinstance(history, list) else []
    for entry in src:
        if not isinstance(entry, dict):
            changed = True
            continue
        raw_date = entry.get("date")
        if not isinstance(raw_date, str):
            changed = True
            continue
        # Normalise to YYYY-MM-DD (tolerate full ISO timestamps).
        norm_date = raw_date[:10]
        if norm_date == today_iso:
            seen_today = True
            existing = entry.get("priceUsd")
            if existing != price:
                out.append({"date": today_iso, "priceUsd": price})
                changed = True
            else:
                out.append({"date": today_iso, "priceUsd": existing})
            continue
        # Keep historical entries as-is, normalised.
        normalised = {"date": norm_date, "priceUsd": entry.get("priceUsd")}
        if normalised != entry:
            changed = True
        out.append(normalised)

    if not seen_today:
        out.append({"date": today_iso, "priceUsd": price})
        changed = True

    out.sort(key=lambda e: e["date"])

    if len(out) > MAX_HISTORY_POINTS:
        out = out[-MAX_HISTORY_POINTS:]
        changed = True

    return out, changed


async def snapshot_prices(
    ctx: dict[str, Any] | None = None,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Persist today's live market price into every card's history.

    Returns ``{"scanned": n, "updated": k, "skipped": s}`` where
    *skipped* counts rows without a usable live market price.
    """
    today = datetime.now(UTC).date()
    scanned = 0
    updated = 0
    skipped = 0

    sm = get_sessionmaker()
    async with sm() as session:
        offset = 0
        while True:
            stmt = select(Card).order_by(Card.id).offset(offset).limit(batch_size)
            rows = (await session.execute(stmt)).scalars().all()
            if not rows:
                break

            batch_updated = 0
            for card in rows:
                scanned += 1
                meta = (
                    card.card_metadata if isinstance(card.card_metadata, dict) else {}
                )
                price = _extract_market_amount(meta)
                if price is None:
                    skipped += 1
                    continue

                history = meta.get("price_history")
                new_history, changed = _upsert_today(history, today, price)
                if not changed:
                    continue

                new_meta = dict(meta)
                new_meta["price_history"] = new_history
                card.card_metadata = new_meta
                flag_modified(card, "card_metadata")
                updated += 1
                batch_updated += 1

            if batch_updated:
                await session.commit()

            if len(rows) < batch_size:
                break
            offset += batch_size

    result = {"scanned": scanned, "updated": updated, "skipped": skipped}
    logger.info("price_snapshot complete: %s", result)
    return result


__all__ = ["DEFAULT_BATCH_SIZE", "MAX_HISTORY_POINTS", "snapshot_prices"]
