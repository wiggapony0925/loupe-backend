"""Seed 50 realistic demo users with portfolios + scanners.

Single source of truth for the persona list lives in
:mod:`documentation.test_personas`. This script consumes that list and
makes the database match it.

Idempotent: re-running the script will not duplicate users, graded
cards, scanners, or card_set / card rows. All seeded accounts use the
prefix ``test+NN@loupe.app`` so they can be purged with:

    DELETE FROM users WHERE email LIKE 'test+%@loupe.app';

Usage:
    python -m scripts.seed_test_users                  # against DATABASE_URL
    DATABASE_URL=... python -m scripts.seed_test_users # explicit
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.auth.passwords import hash_password
from app.db.session import get_sessionmaker
from app.models.card import Card, CardSet
from app.models.collection import Collection, CollectionItem
from app.models.enums import GradeHouseEnum, ScannerTransportEnum, TcgEnum
from app.models.grade import GradedCard
from app.models.scanner import Scanner
from app.models.user import User, UserSettings
from app.utils.logger import get_logger
from documentation.test_personas import (
    DEFAULT_PASSWORD,
    PERSONAS,
    Persona,
)

logger = get_logger("scripts.seed")

# Deterministic seed so re-runs produce identical content.
random.seed(20260516)

#: How many days of daily ``price_history`` to write per Card row.
HISTORY_DAYS = 180


# ─────────────────────────────────────────────────────────────────────────
# Card catalog — keyed by ``archetype``
# ─────────────────────────────────────────────────────────────────────────
CATALOG: dict[str, dict] = {
    "vintage_pokemon": {
        "set": {
            "name": "Base Set",
            # Real pokemontcg.io ``set.id`` so the resolver's strict
            # ``name + set.id + number`` query matches on the first try.
            # The friendly "BS" code we used to seed forced the resolver
            # to fall back through two slower queries and often time out.
            "code": "base1",
            "tcg": TcgEnum.pokemon,
            "release_date": date(1999, 1, 9),
            "total": 102,
        },
        "cards": [
            (
                "Charizard",
                "4/102",
                "Holo Rare",
                1999,
                "https://images.pokemontcg.io/base1/4_hires.png",
            ),
            (
                "Blastoise",
                "2/102",
                "Holo Rare",
                1999,
                "https://images.pokemontcg.io/base1/2_hires.png",
            ),
            (
                "Venusaur",
                "15/102",
                "Holo Rare",
                1999,
                "https://images.pokemontcg.io/base1/15_hires.png",
            ),
            (
                "Pikachu",
                "58/102",
                "Common",
                1999,
                "https://images.pokemontcg.io/base1/58_hires.png",
            ),
            (
                "Machamp",
                "8/102",
                "Holo Rare",
                1999,
                "https://images.pokemontcg.io/base1/8_hires.png",
            ),
            (
                "Alakazam",
                "1/102",
                "Holo Rare",
                1999,
                "https://images.pokemontcg.io/base1/1_hires.png",
            ),
        ],
    },
    "modern_pokemon": {
        "set": {
            "name": "Crown Zenith",
            # Real pokemontcg.io ``set.id``. Crown Zenith also has a
            # parallel "Galarian Gallery" set (``swsh12pt5gg``); the
            # GG-numbered cards in this list will resolve via the
            # resolver's ``name + number`` fallback.
            "code": "swsh12pt5",
            "tcg": TcgEnum.pokemon,
            "release_date": date(2023, 1, 20),
            "total": 230,
        },
        "cards": [
            (
                # GG44/GG70 in Crown Zenith Galarian Gallery is Mewtwo VSTAR;
                # Charizard appears only as the illustration backdrop.
                "Mewtwo VSTAR",
                "GG44/GG70",
                "Trainer Gallery",
                2023,
                "https://images.pokemontcg.io/swsh12pt5gg/GG44_hires.png",
            ),
            (
                "Mew VMAX",
                "GG39/GG70",
                "Trainer Gallery",
                2023,
                "https://images.pokemontcg.io/swsh12pt5gg/GG39_hires.png",
            ),
            (
                "Lugia VSTAR",
                "139/159",
                "Ultra Rare",
                2023,
                "https://images.pokemontcg.io/swsh12pt5/139_hires.png",
            ),
            (
                "Giratina VSTAR",
                "131/159",
                "Ultra Rare",
                2023,
                "https://images.pokemontcg.io/swsh12pt5/131_hires.png",
            ),
            (
                "Arceus VSTAR",
                "184/159",
                "Hyper Rare",
                2023,
                "https://images.pokemontcg.io/swsh12pt5/184_hires.png",
            ),
        ],
    },
    "magic_reserved": {
        "set": {
            "name": "Alpha",
            "code": "LEA",
            "tcg": TcgEnum.magic,
            "release_date": date(1993, 8, 5),
            "total": 295,
        },
        "cards": [
            ("Black Lotus", "232/295", "Rare", 1993, None),
            ("Mox Sapphire", "263/295", "Rare", 1993, None),
            ("Ancestral Recall", "47/295", "Rare", 1993, None),
            ("Time Walk", "85/295", "Rare", 1993, None),
            ("Timetwister", "86/295", "Rare", 1993, None),
        ],
    },
    "yugioh_meta": {
        "set": {
            "name": "Legend of Blue Eyes White Dragon",
            "code": "LOB",
            "tcg": TcgEnum.yugioh,
            "release_date": date(2002, 3, 8),
            "total": 126,
        },
        "cards": [
            ("Blue-Eyes White Dragon", "LOB-001", "Ultra Rare", 2002, None),
            ("Dark Magician", "LOB-005", "Ultra Rare", 2002, None),
            ("Exodia the Forbidden", "LOB-124", "Ultra Rare", 2002, None),
            ("Red-Eyes Black Dragon", "LOB-070", "Ultra Rare", 2002, None),
            ("Summoned Skull", "LOB-053", "Ultra Rare", 2002, None),
        ],
    },
    "sports_basketball": {
        "set": {
            "name": "Prizm Basketball",
            "code": "PRIZM",
            "tcg": TcgEnum.sports,
            "release_date": date(2023, 11, 1),
            "total": 300,
        },
        "cards": [
            ("Victor Wembanyama RC", "136/300", "Rookie", 2023, None),
            ("LeBron James", "1/300", "Base", 2023, None),
            ("Stephen Curry", "23/300", "Base", 2023, None),
            ("Luka Doncic", "33/300", "Base", 2023, None),
            ("Jayson Tatum", "12/300", "Base", 2023, None),
        ],
    },
    "sports_baseball": {
        "set": {
            "name": "Topps Chrome",
            "code": "TC",
            "tcg": TcgEnum.sports,
            "release_date": date(2024, 8, 1),
            "total": 200,
        },
        "cards": [
            ("Shohei Ohtani", "17/200", "Base", 2024, None),
            ("Aaron Judge", "1/200", "Base", 2024, None),
            ("Mookie Betts", "50/200", "Base", 2024, None),
            ("Ronald Acuña Jr.", "100/200", "Base", 2024, None),
            ("Paul Skenes RC", "150/200", "Rookie", 2024, None),
        ],
    },
    "onepiece": {
        "set": {
            "name": "Romance Dawn",
            "code": "OP-01",
            "tcg": TcgEnum.onepiece,
            "release_date": date(2022, 12, 2),
            "total": 121,
        },
        "cards": [
            ("Monkey D. Luffy", "OP01-001", "Leader", 2022, None),
            ("Roronoa Zoro", "OP01-025", "Super Rare", 2022, None),
            ("Nami", "OP01-016", "Rare", 2022, None),
            ("Shanks", "OP01-120", "Secret Rare", 2022, None),
        ],
    },
    "lorcana": {
        "set": {
            "name": "The First Chapter",
            "code": "TFC",
            "tcg": TcgEnum.lorcana,
            "release_date": date(2023, 8, 18),
            "total": 216,
        },
        "cards": [
            ("Elsa - Snow Queen", "42/204", "Legendary", 2023, None),
            ("Mickey Mouse - Brave Tiny", "1/204", "Super Rare", 2023, None),
            ("Maleficent - Sorceress", "100/204", "Rare", 2023, None),
            ("Stitch - Carefree Surfer", "180/204", "Common", 2023, None),
        ],
    },
}

#: Rough realistic PSA-10 USD anchor per card name.
VALUES_USD: dict[str, Decimal] = {
    "Charizard": Decimal("8500"),
    "Blastoise": Decimal("950"),
    "Venusaur": Decimal("700"),
    "Pikachu": Decimal("125"),
    "Machamp": Decimal("180"),
    "Alakazam": Decimal("450"),
    "Charizard VSTAR": Decimal("220"),
    "Mew VMAX": Decimal("180"),
    "Lugia VSTAR": Decimal("85"),
    "Giratina VSTAR": Decimal("65"),
    "Arceus VSTAR": Decimal("75"),
    "Black Lotus": Decimal("420000"),
    "Mox Sapphire": Decimal("85000"),
    "Ancestral Recall": Decimal("48000"),
    "Time Walk": Decimal("40000"),
    "Timetwister": Decimal("18000"),
    "Blue-Eyes White Dragon": Decimal("2200"),
    "Dark Magician": Decimal("1500"),
    "Exodia the Forbidden": Decimal("950"),
    "Red-Eyes Black Dragon": Decimal("850"),
    "Summoned Skull": Decimal("280"),
    "Victor Wembanyama RC": Decimal("1800"),
    "LeBron James": Decimal("450"),
    "Stephen Curry": Decimal("220"),
    "Luka Doncic": Decimal("180"),
    "Jayson Tatum": Decimal("95"),
    "Shohei Ohtani": Decimal("380"),
    "Aaron Judge": Decimal("220"),
    "Mookie Betts": Decimal("110"),
    "Ronald Acuña Jr.": Decimal("140"),
    "Paul Skenes RC": Decimal("280"),
    "Monkey D. Luffy": Decimal("65"),
    "Roronoa Zoro": Decimal("85"),
    "Nami": Decimal("45"),
    "Shanks": Decimal("320"),
    "Elsa - Snow Queen": Decimal("180"),
    "Mickey Mouse - Brave Tiny": Decimal("220"),
    "Maleficent - Sorceress": Decimal("85"),
    "Stitch - Carefree Surfer": Decimal("25"),
}

#: Grade-house mix per archetype (used unless the persona overrides).
HOUSE_MIX: dict[str, list[tuple[str, float]]] = {
    "vintage_pokemon": [("psa", 0.7), ("loupe", 0.3)],
    "modern_pokemon": [("psa", 0.5), ("loupe", 0.5)],
    "magic_reserved": [("bgs", 0.6), ("psa", 0.4)],
    "yugioh_meta": [("psa", 0.6), ("cgc", 0.4)],
    "sports_basketball": [("psa", 0.8), ("bgs", 0.2)],
    "sports_baseball": [("psa", 0.7), ("sgc", 0.3)],
    "onepiece": [("loupe", 0.7), ("cgc", 0.3)],
    "lorcana": [("loupe", 1.0)],
    "mixed": [("psa", 0.5), ("bgs", 0.2), ("cgc", 0.15), ("loupe", 0.15)],
    "grail": [("psa", 1.0)],
}


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _grade_for(avg: float) -> Decimal:
    """Sample a grade clamped to [3.0, 10.0] centred at *avg*."""
    if avg >= 10.0:
        # "all gem-mint" persona — return a hard 10.
        return Decimal("10.0")
    g = max(3.0, min(10.0, random.gauss(avg, 1.0)))
    return Decimal(str(round(g * 2) / 2))


def _value_for(card_name: str, grade: Decimal) -> Decimal:
    base = VALUES_USD.get(card_name, Decimal("50"))
    multiplier = {
        Decimal("10.0"): Decimal("1.00"),
        Decimal("9.5"): Decimal("0.55"),
        Decimal("9.0"): Decimal("0.30"),
        Decimal("8.5"): Decimal("0.18"),
        Decimal("8.0"): Decimal("0.12"),
        Decimal("7.5"): Decimal("0.08"),
        Decimal("7.0"): Decimal("0.06"),
    }.get(grade, Decimal("0.04"))
    return (base * multiplier).quantize(Decimal("0.01"))


def _pick_house(mix: list[tuple[str, float]]) -> GradeHouseEnum:
    r = random.random()
    cum = 0.0
    for name, prob in mix:
        cum += prob
        if r <= cum:
            return GradeHouseEnum(name)
    return GradeHouseEnum(mix[-1][0])


def _seeded_walk(anchor: float, card_id: str, days: int) -> list[float]:
    """Reverse random walk that lands on *anchor* on the last day."""
    rng = random.Random(card_id)
    steps = [rng.uniform(-0.02, 0.02) for _ in range(days)]
    cur = anchor
    out = [anchor]
    for s in reversed(steps[:-1]):
        cur = cur / (1.0 + s)
        out.append(max(cur, anchor * 0.05))
    out.reverse()
    return [round(v, 2) for v in out]


# ─────────────────────────────────────────────────────────────────────────
# Catalog & user creation
# ─────────────────────────────────────────────────────────────────────────
async def _get_or_create_catalog(db) -> dict[str, list[Card]]:
    """Ensure every archetype's CardSet + Card rows exist."""
    out: dict[str, list[Card]] = {}
    for archetype, spec in CATALOG.items():
        set_spec = spec["set"]
        cset = (
            await db.execute(select(CardSet).where(CardSet.code == set_spec["code"]))
        ).scalar_one_or_none()
        if cset is None:
            cset = CardSet(
                tcg=set_spec["tcg"],
                name=set_spec["name"],
                code=set_spec["code"],
                release_date=set_spec.get("release_date"),
                total_cards=set_spec.get("total"),
            )
            db.add(cset)
            await db.flush()

        cards: list[Card] = []
        for name, number, rarity, year, image_url in spec["cards"]:
            card = (
                await db.execute(
                    select(Card).where(
                        Card.set_id == cset.id,
                        Card.name == name,
                        Card.number == number,
                    )
                )
            ).scalar_one_or_none()
            if card is None:
                card = Card(
                    set_id=cset.id,
                    tcg=set_spec["tcg"],
                    name=name,
                    number=number,
                    rarity=rarity,
                    year=year,
                    image_url=image_url,
                )
                db.add(card)
                await db.flush()
            cards.append(card)
        out[archetype] = cards
    await db.commit()
    return out


def _cards_for(persona: Persona, catalog: dict[str, list[Card]]) -> list[Card]:
    """Resolve the card pool a persona should draw from."""
    if persona.archetype == "empty":
        return []
    if persona.archetype == "mixed":
        return [c for cs in catalog.values() for c in cs]
    if persona.archetype == "grail":
        # First card of magic_reserved is Black Lotus.
        return [catalog["magic_reserved"][0]]
    return catalog[persona.archetype]


async def _get_or_create_user(db, persona: Persona) -> tuple[User, bool]:
    """Find-or-create the user row; return (user, created_now)."""
    existing = (
        await db.execute(select(User).where(User.email == persona.email))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    created_at = datetime.now(UTC) - timedelta(days=persona.tenure_days)
    kwargs: dict = {
        "email": persona.email,
        "display_name": persona.name,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if persona.auth == "password":
        kwargs["password_hash"] = hash_password(DEFAULT_PASSWORD)
    elif persona.auth == "apple":
        # Stable subject derived from email so re-creates keep the same SSO id.
        kwargs["apple_subject"] = (
            "apple-" + hashlib.sha256(persona.email.encode()).hexdigest()[:32]
        )
    elif persona.auth == "google":
        kwargs["google_subject"] = (
            "google-" + hashlib.sha256(persona.email.encode()).hexdigest()[:32]
        )

    user = User(**kwargs)
    db.add(user)
    await db.flush()
    db.add(UserSettings(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    logger.info("Created user %s (%s)", persona.email, persona.archetype)
    return user, True


# ─────────────────────────────────────────────────────────────────────────
# Vault + scanner seeding
# ─────────────────────────────────────────────────────────────────────────
def _scanner_specs(persona: Persona) -> list[dict]:
    """Resolve the list of Scanner rows to create for a persona."""
    now = datetime.now(UTC)
    profile = persona.scanner_profile

    if profile == "none":
        return []

    base_id = persona.id
    first_name = persona.name.split()[0]

    def _spec(
        idx: int,
        transport: ScannerTransportEnum,
        age_minutes: int,
        fw: str = "1.4.2",
    ) -> dict:
        return {
            "device_id": f"loupe-{base_id:02d}-{idx}",
            "name": f"{first_name}'s Loupe {idx}"
            if idx > 1
            else f"{first_name}'s Loupe",
            "firmware_version": fw,
            "last_seen_at": now - timedelta(minutes=age_minutes),
            "transport": transport,
            "is_active": age_minutes < 60 * 24,  # idle >24h ⇒ inactive
        }

    if profile == "fresh":
        return [_spec(1, ScannerTransportEnum.ble, age_minutes=2)]
    if profile == "active":
        return [_spec(1, ScannerTransportEnum.ble, age_minutes=5)]
    if profile == "offline":
        return [
            _spec(1, ScannerTransportEnum.offline, age_minutes=60 * 24 * 30, fw="1.2.0")
        ]
    if profile == "dual":
        return [
            _spec(1, ScannerTransportEnum.ble, age_minutes=5),
            _spec(2, ScannerTransportEnum.wifi, age_minutes=20),
        ]
    if profile == "multi":
        return [
            _spec(1, ScannerTransportEnum.ble, age_minutes=5),
            _spec(2, ScannerTransportEnum.wifi, age_minutes=30),
            _spec(
                3, ScannerTransportEnum.offline, age_minutes=60 * 24 * 14, fw="1.3.0"
            ),
        ]
    if profile == "fleet":
        return [
            _spec(1, ScannerTransportEnum.ble, age_minutes=5),
            _spec(2, ScannerTransportEnum.ble, age_minutes=15),
            _spec(3, ScannerTransportEnum.wifi, age_minutes=30),
            _spec(4, ScannerTransportEnum.wifi, age_minutes=120),
            _spec(5, ScannerTransportEnum.offline, age_minutes=60 * 24 * 7, fw="1.3.0"),
        ]
    return []


async def _seed_scanners(db, user: User, persona: Persona) -> int:
    existing = (
        (await db.execute(select(Scanner).where(Scanner.owner_id == user.id)))
        .scalars()
        .all()
    )
    have_ids = {s.device_id for s in existing}
    created = 0
    for spec in _scanner_specs(persona):
        if spec["device_id"] in have_ids:
            continue
        db.add(Scanner(owner_id=user.id, **spec))
        created += 1
    if created:
        await db.commit()
    return created


async def _seed_vault(
    db, user: User, persona: Persona, catalog: dict[str, list[Card]]
) -> int:
    if persona.vault_size == 0:
        return 0

    existing_grade_count = len(
        (await db.execute(select(GradedCard).where(GradedCard.user_id == user.id)))
        .scalars()
        .all()
    )
    if existing_grade_count > 0:
        return existing_grade_count

    cards = _cards_for(persona, catalog)
    if not cards:
        return 0

    # Re-seed the RNG per persona so different accounts get different
    # vault contents instead of every test user showing the SAME 200
    # cards and the SAME $144,512.60 total. Still deterministic across
    # re-runs (same persona name → same seed → same vault).
    random.seed(f"loupe-vault::{persona.name}")

    house_mix = HOUSE_MIX.get(persona.archetype, HOUSE_MIX["mixed"])

    # Spread graded_at across the persona's tenure (capped at 180d so the
    # history chart always has dense recent points).
    window_days = max(1, min(persona.tenure_days or 1, 180))
    base = datetime.now(UTC) - timedelta(days=window_days)

    for _ in range(persona.vault_size):
        card = random.choice(cards)
        grade = _grade_for(persona.avg_grade)
        value = _value_for(card.name, grade)
        graded_at = base + timedelta(
            seconds=random.randint(0, window_days * 24 * 60 * 60 - 1)
        )
        gc = GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=grade,
            house=_pick_house(house_mix),
            estimated_value_usd=value,
            graded_at=graded_at,
            subgrades={
                "centering": float(grade) - random.uniform(0, 0.5),
                "corners": float(grade) - random.uniform(0, 0.3),
                "edges": float(grade) - random.uniform(0, 0.3),
                "surface": float(grade) - random.uniform(0, 0.4),
            },
        )
        db.add(gc)

    coll = Collection(
        user_id=user.id,
        name="My Vault",
        description=f"{persona.name}'s personal collection",
        color="#00F59B",
        is_public=False,
    )
    db.add(coll)
    await db.flush()
    grades = (
        (
            await db.execute(
                select(GradedCard)
                .where(GradedCard.user_id == user.id)
                .order_by(GradedCard.graded_at.desc())
                .limit(3)
            )
        )
        .scalars()
        .all()
    )
    for gc in grades:
        db.add(CollectionItem(collection_id=coll.id, graded_card_id=gc.id))
    await db.commit()
    return persona.vault_size


async def _enrich_price_history(db) -> int:
    """Backfill ``metadata['price_history']`` for cards owned by demo users."""
    stmt = (
        select(Card, GradedCard)
        .join(GradedCard, GradedCard.card_id == Card.id)
        .join(User, User.id == GradedCard.user_id)
        .where(User.email.like("test+%@loupe.app"))
    )
    seen: set[str] = set()
    touched = 0
    for card, gc in (await db.execute(stmt)).all():
        if str(card.id) in seen:
            continue
        seen.add(str(card.id))
        meta = dict(card.card_metadata) if isinstance(card.card_metadata, dict) else {}
        if meta.get("price_history"):
            continue
        anchor = float(gc.estimated_value_usd or Decimal("50"))
        walk = _seeded_walk(anchor, str(card.id), HISTORY_DAYS)
        today = datetime.now(UTC).date()
        start = today - timedelta(days=HISTORY_DAYS - 1)
        meta["price_history"] = [
            {"date": (start + timedelta(days=i)).isoformat(), "priceUsd": p}
            for i, p in enumerate(walk)
        ]
        card.card_metadata = meta
        flag_modified(card, "card_metadata")
        touched += 1
    if touched:
        await db.commit()
    return touched


# ─────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────
async def seed_persona(db, persona: Persona, catalog: dict[str, list[Card]]) -> dict:
    user, created = await _get_or_create_user(db, persona)
    grades = await _seed_vault(db, user, persona, catalog)
    scanners = await _seed_scanners(db, user, persona)
    return {
        "email": persona.email,
        "created": created,
        "grades": grades,
        "scanners": scanners,
    }


async def main() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        catalog = await _get_or_create_catalog(db)
        summary: list[dict] = []
        for persona in PERSONAS:
            row = await seed_persona(db, persona, catalog)
            summary.append(row)
            logger.info(
                "✓ #%02d %-22s grades=%-4d scanners=%d %s",
                persona.id,
                persona.email,
                row["grades"],
                row["scanners"],
                "(new)" if row["created"] else "(exists)",
            )

        touched = await _enrich_price_history(db)

        total_users = len(summary)
        total_grades = sum(r["grades"] for r in summary)
        total_scanners = sum(r["scanners"] for r in summary)
        all_grades = (await db.execute(select(GradedCard))).scalars().all()
        total_value = sum((g.estimated_value_usd or Decimal("0")) for g in all_grades)

        print()
        print(f"✓ {total_users} personas seeded")
        print(f"  + {total_grades:>5} graded cards created this run")
        print(f"  + {total_scanners:>5} scanners created this run")
        print(f"  + {touched:>5} cards enriched with price_history")
        print(f"  Σ ${total_value:>14,.2f} portfolio value across all demo users")
        print()
        print(f"Login: any email + password '{DEFAULT_PASSWORD}'")
        print("Docs:  /test-users (linked from /api-docs)")


if __name__ == "__main__":
    asyncio.run(main())
