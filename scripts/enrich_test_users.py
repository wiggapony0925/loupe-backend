"""Backfill price_history + pricing_summary + scanner rows for the seeded test users.

The original ``seed_test_users.py`` creates users, cards, and ``GradedCard``
rows with ``estimated_value_usd`` populated, but it doesn't write the
``metadata['price_history']`` payload that
``app.services.collection.portfolio_service`` reads to draw the Command Center chart
and Top Movers sparklines, doesn't populate
``metadata['pricing_summary']['market']['amount']`` (the live market
price the vault summary + headline + ``price_snapshot`` task consume),
and doesn't create a ``Scanner`` row so ``GET /v1/scanners/status`` can
return a device.

This script is idempotent — it re-runs the deterministic walk only
when the latest recorded ``price_history`` entry is older than
yesterday. Cards already up to date are skipped. Re-running after the
nightly ``price_snapshot`` job is therefore a no-op.

* For every ``Card`` referenced by a ``GradedCard`` whose owner is a
  seeded ``test+NN@loupe.app`` user, write a 180-day daily
  ``price_history`` series anchored at that grade's
  ``estimated_value_usd``, and pin ``pricing_summary.market.amount``
  to the walk's terminal value so the live total moves with the
  series.
* For every seeded user without a ``Scanner``, create one.

Run with:

    python -m scripts.enrich_test_users
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import get_sessionmaker
from app.models.card import Card
from app.models.grade import GradedCard
from app.models.scanner import Scanner, ScannerTransportEnum
from app.models.user import User
from app.utils.logger import get_logger

logger = get_logger("scripts.enrich")

#: How many days of daily history to write per card.
HISTORY_DAYS = 180


def _seeded_walk(anchor: float, card_id: str, days: int) -> list[float]:
    """Reverse random walk that lands on *anchor* on the last day."""
    rng = random.Random(card_id)
    # Daily relative step in [-2%, +2%].
    steps = [rng.uniform(-0.02, 0.02) for _ in range(days)]
    out = [anchor]
    cur = anchor
    for s in reversed(steps[:-1]):
        cur = cur / (1.0 + s)
        out.append(max(cur, anchor * 0.05))
    out.reverse()
    return [round(v, 2) for v in out]


async def enrich_price_history() -> dict[str, int]:
    sm = get_sessionmaker()
    touched = 0
    skipped = 0
    async with sm() as db:
        # Cards that belong to at least one seeded user's vault.
        stmt = (
            select(Card, GradedCard)
            .join(GradedCard, GradedCard.card_id == Card.id)
            .join(User, User.id == GradedCard.user_id)
            .where(User.email.like("test+%@loupe.app"))
        )
        seen: set[str] = set()
        for card, gc in (await db.execute(stmt)).all():
            key = str(card.id)
            if key in seen:
                continue
            seen.add(key)
            meta = (
                dict(card.card_metadata) if isinstance(card.card_metadata, dict) else {}
            )
            existing = meta.get("price_history") or []
            # Re-generate whenever the latest entry is older than yesterday.
            # The walk is deterministic per card.id, so re-runs that already
            # land on today are no-ops at the cost of one date comparison.
            latest_date: str | None = None
            if isinstance(existing, list) and existing:
                latest = existing[-1]
                if isinstance(latest, dict):
                    latest_date = latest.get("date")
            today = datetime.now(UTC).date()
            yesterday_iso = (today - timedelta(days=1)).isoformat()
            if latest_date and latest_date >= yesterday_iso:
                skipped += 1
                continue
            anchor = gc.estimated_value_usd or Decimal("50")
            walk = _seeded_walk(float(anchor), str(card.id), HISTORY_DAYS)
            start = today - timedelta(days=HISTORY_DAYS - 1)
            meta["price_history"] = [
                {"date": (start + timedelta(days=i)).isoformat(), "priceUsd": p}
                for i, p in enumerate(walk)
            ]
            # Pin the live market amount to today's walk endpoint so the
            # vault summary, Command Center hero, and price_snapshot job
            # all see a consistent "live" price for this card. Without
            # this, _current_market_value() returns None and the live
            # total falls back to GradedCard.estimated_value_usd (frozen
            # at scan time), which won't move day-to-day.
            today_price = walk[-1]
            pricing = (
                dict(meta.get("pricing_summary"))
                if isinstance(meta.get("pricing_summary"), dict)
                else {}
            )
            pricing["market"] = {
                "amount": today_price,
                "currency": "USD",
                "source": "seed",
                "as_of": today.isoformat(),
            }
            meta["pricing_summary"] = pricing
            card.card_metadata = meta
            flag_modified(card, "card_metadata")
            touched += 1
        await db.commit()
    logger.info("price_history enrichment: touched=%d, skipped=%d", touched, skipped)
    return {"touched": touched, "skipped": skipped}


async def enrich_scanners() -> dict[str, int]:
    sm = get_sessionmaker()
    created = 0
    skipped = 0
    async with sm() as db:
        users = (
            (await db.execute(select(User).where(User.email.like("test+%@loupe.app"))))
            .scalars()
            .all()
        )
        for u in users:
            existing = (
                await db.execute(
                    select(Scanner).where(Scanner.owner_id == u.id).limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            short = str(u.id).split("-")[0]
            db.add(
                Scanner(
                    owner_id=u.id,
                    device_id=f"loupe-{short}",
                    name=f"{(u.display_name or 'Loupe').split()[0]}'s Loupe",
                    firmware_version="1.4.2",
                    last_seen_at=datetime.now(UTC) - timedelta(minutes=5),
                    transport=ScannerTransportEnum.ble,
                    is_active=True,
                )
            )
            created += 1
        await db.commit()
    logger.info("scanner enrichment: created=%d, skipped=%d", created, skipped)
    return {"created": created, "skipped": skipped}


async def main() -> None:
    h = await enrich_price_history()
    s = await enrich_scanners()
    print(
        f"price_history: touched={h['touched']} skipped={h['skipped']} | "
        f"scanners: created={s['created']} skipped={s['skipped']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
