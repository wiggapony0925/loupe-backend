"""Backfill price_history + scanner rows for the seeded test users.

The original ``seed_test_users.py`` creates users, cards, and ``GradedCard``
rows with ``estimated_value_usd`` populated, but it doesn't write the
``metadata['price_history']`` payload that
``app.services.collection.portfolio_service`` reads to draw the Command Center chart
and Top Movers sparklines, and it doesn't create a ``Scanner`` row so
``GET /v1/scanners/status`` can return a device.

This script is idempotent — it only fills missing data:

* For every ``Card`` referenced by a ``GradedCard`` whose owner is a
  seeded ``test+NN@loupe.app`` user, write a 180-day daily ``price_history``
  series anchored at that grade's ``estimated_value_usd``. The walk is
  deterministic per ``card.id`` so repeated runs produce identical output.
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
            if meta.get("price_history"):
                skipped += 1
                continue
            anchor = gc.estimated_value_usd or Decimal("50")
            # Deterministic per card.id.
            random.seed(str(card.id))
            walk = _seeded_walk(float(anchor), str(card.id), HISTORY_DAYS)
            today = datetime.now(UTC).date()
            start = today - timedelta(days=HISTORY_DAYS - 1)
            meta["price_history"] = [
                {"date": (start + timedelta(days=i)).isoformat(), "priceUsd": p}
                for i, p in enumerate(walk)
            ]
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
                await db.execute(select(Scanner).where(Scanner.owner_id == u.id))
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
