"""Seed one existing user into a heavy, showcase-worthy power user.

Populates real cards (via the live catalog search + resolver) into themed
collections, plus a watchlist and price alerts — so the account shows off the
app's full potential: a multi-TCG vault, an Umbreon master set, graded gems,
P/L, watchlist, and alerts.

Idempotent: every holding it creates is tagged ``seed:v2``; re-running skips a
user that already has seeded holdings (use ``--force`` to add another wave).

Run against a DB (prod via the Cloud SQL proxy):

    cloud-sql-proxy loupe-app-56235:us-central1:loupe-pg --port 5433 &
    DATABASE_URL='postgresql+asyncpg://loupe:PASS@127.0.0.1:5433/loupe' \
        .venv/bin/python scripts/seed_power_user.py \
        --user adc50f6a-98eb-4700-a533-4793fee46c16
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.db.session import get_sessionmaker
from app.models.enums import GradeHouseEnum, PriceAlertCondition, RawConditionEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.collection import CollectionCreate
from app.schemas.grade import GradedCardCreate
from app.schemas.price_alert import PriceAlertCreate
from app.services.catalog import card_search_service
from app.services.collection import (
    collection_service,
    graded_card_service,
    watchlist_service,
)
from app.services.market import price_alert_service

SEED_TAG = "seed:v2"
random.seed(2026)


# (collection name, color, [(query, tcg, take)]) — real cards pulled live.
GROUPS: list[tuple[str, str, list[tuple[str, str, int]]]] = [
    (
        "Umbreon Master Set",
        "#3b3b6d",
        [("umbreon", "pokemon", 18)],
    ),
    (
        "Vintage Charizard Vault",
        "#e2571e",
        [("charizard", "pokemon", 8)],
    ),
    (
        "Eeveelution Rainbow",
        "#f5a623",
        [
            ("vaporeon", "pokemon", 2),
            ("jolteon", "pokemon", 2),
            ("flareon", "pokemon", 2),
            ("espeon", "pokemon", 2),
            ("leafeon", "pokemon", 2),
            ("glaceon", "pokemon", 2),
            ("sylveon", "pokemon", 2),
        ],
    ),
    (
        "Magic: The Icons",
        "#1f6feb",
        [
            ("black lotus", "magic", 1),
            ("mox sapphire", "magic", 1),
            ("lightning bolt", "magic", 3),
            ("tarmogoyf", "magic", 2),
        ],
    ),
    (
        "Yu-Gi-Oh! Classics",
        "#8957e5",
        [
            ("dark magician", "yugioh", 3),
            ("blue-eyes white dragon", "yugioh", 3),
        ],
    ),
]

# Chase cards to also watch (query, tcg).
WATCH: list[tuple[str, str]] = [
    ("umbreon gold star", "pokemon"),
    ("charizard base set", "pokemon"),
    ("pikachu illustrator", "pokemon"),
    ("black lotus", "magic"),
    ("blue-eyes white dragon", "yugioh"),
    ("moonbreon", "pokemon"),
]

_HOUSES = [
    (GradeHouseEnum.psa, 0.42),
    (GradeHouseEnum.loupe, 0.28),  # raw
    (GradeHouseEnum.cgc, 0.12),
    (GradeHouseEnum.bgs, 0.12),
    (GradeHouseEnum.sgc, 0.06),
]
_RAW_CONDITIONS = [
    RawConditionEnum.nm,
    RawConditionEnum.nm,
    RawConditionEnum.lp,
]


def _pick_house() -> GradeHouseEnum:
    r, cum = random.random(), 0.0
    for house, w in _HOUSES:
        cum += w
        if r <= cum:
            return house
    return GradeHouseEnum.psa


def _grade_for(house: GradeHouseEnum) -> Decimal:
    if house is GradeHouseEnum.loupe:
        return Decimal("0")
    return Decimal(str(random.choice([10, 10, 9.5, 9, 9, 8.5, 8])))


def _market_of(card: dict) -> float | None:
    ps = card.get("pricing_summary") or {}
    for k in ("market", "high", "mid", "low"):
        v = ps.get(k)
        if isinstance(v, dict) and v.get("amount"):
            try:
                return float(v["amount"])
            except (TypeError, ValueError):
                continue
    return None


def _value_for(card: dict, house: GradeHouseEnum, grade: Decimal) -> Decimal:
    base = _market_of(card) or random.uniform(15, 220)
    # Grade premium so slabbed gems read as more valuable than raw.
    mult = 1.0
    if house is not GradeHouseEnum.loupe:
        mult = {10: 6.0, 9.5: 3.2, 9: 2.0, 8.5: 1.4, 8: 1.1}.get(float(grade), 1.5)
    return Decimal(str(round(base * mult * random.uniform(0.9, 1.25), 2)))


async def _already_seeded(db, user: User) -> bool:
    n = (
        await db.execute(
            select(func.count(GradedCard.id)).where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
                GradedCard.tags.isnot(None),
            )
        )
    ).scalar() or 0
    # Cheap heuristic: any tagged holding ⇒ assume seeded.
    if not n:
        return False
    rows = (
        (
            await db.execute(
                select(GradedCard.tags).where(GradedCard.user_id == user.id).limit(500)
            )
        )
        .scalars()
        .all()
    )
    return any(SEED_TAG in (t or []) for t in rows)


async def _find_cards(query: str, tcg: str, take: int) -> list[dict]:
    try:
        body = await card_search_service.search_cards(
            q=query, tcg=tcg, limit=max(take, 8)
        )
    except Exception as exc:
        print(f"    ! search failed {query!r} ({tcg}): {exc}")
        return []
    results = [r for r in (body.get("results") or []) if r.get("id")]
    return results[:take]


async def _seed_holding(db, user: User, card: dict) -> uuid.UUID | None:
    house = _pick_house()
    grade = _grade_for(house)
    value = _value_for(card, house, grade)
    # Give ~70% a cost basis so P/L is meaningful (some up, some down).
    cost = None
    if random.random() < 0.7:
        cost = Decimal(str(round(float(value) * random.uniform(0.45, 1.15), 2)))
    payload = GradedCardCreate(
        upstream_id=card["id"],
        grade=grade,
        house=house,
        condition=(
            random.choice(_RAW_CONDITIONS) if house is GradeHouseEnum.loupe else None
        ),
        estimated_value_usd=value,
        purchase_price_usd=cost,
        purchase_date=date.today() - timedelta(days=random.randint(20, 900)),
        tags=[SEED_TAG],
    )
    try:
        row = await graded_card_service.create(db, user, payload)
        return row.id
    except Exception as exc:
        print(f"    ! holding failed {card.get('name')!r}: {exc}")
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="User UUID to seed")
    ap.add_argument("--force", action="store_true", help="Seed again even if tagged")
    args = ap.parse_args()

    maker = get_sessionmaker()
    async with maker() as db:
        user = (
            await db.execute(select(User).where(User.id == uuid.UUID(args.user)))
        ).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"User {args.user} not found")
        print(f"Seeding {user.email} ({user.id})")

        if not args.force and await _already_seeded(db, user):
            print("Already seeded (has seed:v2 holdings) — pass --force to add more.")
            return

        total_cards = 0
        for name, color, specs in GROUPS:
            print(f"  · {name}")
            holding_ids: list[uuid.UUID] = []
            for query, tcg, take in specs:
                for card in await _find_cards(query, tcg, take):
                    gid = await _seed_holding(db, user, card)
                    if gid:
                        holding_ids.append(gid)
            if not holding_ids:
                print("    (no cards resolved — skipping collection)")
                continue
            coll = await collection_service.create(
                db, user, CollectionCreate(name=name, color=color)
            )
            for gid in holding_ids:
                await collection_service.add_item(db, user, coll.id, gid)
            total_cards += len(holding_ids)
            print(f"    → {len(holding_ids)} cards in “{name}”")

        # Watchlist — chase cards.
        watched = 0
        for query, tcg in WATCH:
            cards = await _find_cards(query, tcg, 1)
            if cards:
                try:
                    await watchlist_service.add(db, user, cards[0]["id"])
                    watched += 1
                except Exception as exc:
                    print(f"    ! watch failed {query!r}: {exc}")

        # Price alerts on a few held cards.
        alerts = 0
        held = (
            (
                await db.execute(
                    select(GradedCard)
                    .where(GradedCard.user_id == user.id, GradedCard.tags.isnot(None))
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        for gc in held:
            val = float(gc.estimated_value_usd or 100)
            above = random.random() < 0.5
            try:
                await price_alert_service.create(
                    db,
                    user,
                    PriceAlertCreate(
                        card_id=str(gc.card_id),
                        condition=(
                            PriceAlertCondition.above
                            if above
                            else PriceAlertCondition.below
                        ),
                        threshold_usd=Decimal(
                            str(round(val * (1.25 if above else 0.8), 2))
                        ),
                        note=(
                            "Target hit — consider selling" if above else "Buy the dip"
                        ),
                    ),
                )
                alerts += 1
            except Exception as exc:
                print(f"    ! alert failed: {exc}")

        await db.commit()

        total = (
            await db.execute(
                select(
                    func.count(GradedCard.id),
                    func.coalesce(func.sum(GradedCard.estimated_value_usd), 0),
                ).where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
            )
        ).one()
        print(
            f"\nDone at {datetime.now(UTC):%H:%M:%S}. "
            f"+{total_cards} cards this run · {watched} watchlisted · {alerts} alerts. "
            f"Vault now: {int(total[0])} cards, ${float(total[1]):,.2f}."
        )


if __name__ == "__main__":
    asyncio.run(main())
