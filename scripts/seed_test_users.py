"""Seed 10+ realistic test users with portfolios.

Idempotent: re-running this script will not duplicate users or graded cards.
All seeded accounts use the prefix ``test+##@loupe.app`` so they can be
trivially purged later with:

    DELETE FROM users WHERE email LIKE 'test+%@loupe.app';

Usage:
    python -m scripts.seed_test_users                  # against DATABASE_URL
    DATABASE_URL=... python -m scripts.seed_test_users # explicit
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.card import Card, CardSet
from app.models.collection import Collection, CollectionItem
from app.models.enums import GradeHouseEnum, TcgEnum
from app.models.grade import GradedCard
from app.models.user import User, UserSettings
from app.services.user_service import create_with_password, get_by_email
from app.utils.logger import get_logger

logger = get_logger("scripts.seed")

# ── Deterministic seed for reproducibility ──────────────────────────────
random.seed(20260516)

DEFAULT_PASSWORD = "Loupe2026!"

# ── 10 fictional collectors with personalities ──────────────────────────
TEST_USERS: list[dict[str, str]] = [
    {"email": "test+01@loupe.app", "name": "Alex Rivera",     "archetype": "vintage_pokemon"},
    {"email": "test+02@loupe.app", "name": "Mira Tanaka",     "archetype": "modern_pokemon"},
    {"email": "test+03@loupe.app", "name": "Devon Brooks",    "archetype": "magic_reserved"},
    {"email": "test+04@loupe.app", "name": "Priya Patel",     "archetype": "yugioh_meta"},
    {"email": "test+05@loupe.app", "name": "Sam Okafor",      "archetype": "sports_basketball"},
    {"email": "test+06@loupe.app", "name": "Jordan Klein",    "archetype": "sports_baseball"},
    {"email": "test+07@loupe.app", "name": "Lin Wei",         "archetype": "onepiece"},
    {"email": "test+08@loupe.app", "name": "Hannah Schmidt",  "archetype": "lorcana"},
    {"email": "test+09@loupe.app", "name": "Marcus Thompson", "archetype": "mixed_high_end"},
    {"email": "test+10@loupe.app", "name": "Riley Anderson",  "archetype": "mixed_beginner"},
]

# ── Card catalog: realistic names + image URLs (Pokémon TCG free CDN) ───
CATALOG: dict[str, dict] = {
    "vintage_pokemon": {
        "set": {"name": "Base Set", "code": "BS", "tcg": TcgEnum.pokemon,
                "release_date": date(1999, 1, 9), "total": 102},
        "cards": [
            ("Charizard",  "4/102",   "Holo Rare",   1999, "https://images.pokemontcg.io/base1/4_hires.png"),
            ("Blastoise",  "2/102",   "Holo Rare",   1999, "https://images.pokemontcg.io/base1/2_hires.png"),
            ("Venusaur",   "15/102",  "Holo Rare",   1999, "https://images.pokemontcg.io/base1/15_hires.png"),
            ("Pikachu",    "58/102",  "Common",      1999, "https://images.pokemontcg.io/base1/58_hires.png"),
            ("Machamp",    "8/102",   "Holo Rare",   1999, "https://images.pokemontcg.io/base1/8_hires.png"),
            ("Alakazam",   "1/102",   "Holo Rare",   1999, "https://images.pokemontcg.io/base1/1_hires.png"),
        ],
    },
    "modern_pokemon": {
        "set": {"name": "Crown Zenith", "code": "CRZ", "tcg": TcgEnum.pokemon,
                "release_date": date(2023, 1, 20), "total": 230},
        "cards": [
            ("Charizard VSTAR",     "GG44/GG70", "Trainer Gallery", 2023, "https://images.pokemontcg.io/swsh12pt5gg/GG44_hires.png"),
            ("Mew VMAX",            "GG39/GG70", "Trainer Gallery", 2023, "https://images.pokemontcg.io/swsh12pt5gg/GG39_hires.png"),
            ("Lugia VSTAR",         "139/159",   "Ultra Rare",      2023, "https://images.pokemontcg.io/swsh12pt5/139_hires.png"),
            ("Giratina VSTAR",      "131/159",   "Ultra Rare",      2023, "https://images.pokemontcg.io/swsh12pt5/131_hires.png"),
            ("Arceus VSTAR",        "184/159",   "Hyper Rare",      2023, "https://images.pokemontcg.io/swsh12pt5/184_hires.png"),
        ],
    },
    "magic_reserved": {
        "set": {"name": "Alpha", "code": "LEA", "tcg": TcgEnum.magic,
                "release_date": date(1993, 8, 5), "total": 295},
        "cards": [
            ("Black Lotus",       "232/295", "Rare", 1993, None),
            ("Mox Sapphire",      "263/295", "Rare", 1993, None),
            ("Ancestral Recall",  "47/295",  "Rare", 1993, None),
            ("Time Walk",         "85/295",  "Rare", 1993, None),
            ("Timetwister",       "86/295",  "Rare", 1993, None),
        ],
    },
    "yugioh_meta": {
        "set": {"name": "Legend of Blue Eyes White Dragon", "code": "LOB", "tcg": TcgEnum.yugioh,
                "release_date": date(2002, 3, 8), "total": 126},
        "cards": [
            ("Blue-Eyes White Dragon", "LOB-001", "Ultra Rare",   2002, None),
            ("Dark Magician",          "LOB-005", "Ultra Rare",   2002, None),
            ("Exodia the Forbidden",   "LOB-124", "Ultra Rare",   2002, None),
            ("Red-Eyes Black Dragon",  "LOB-070", "Ultra Rare",   2002, None),
            ("Summoned Skull",         "LOB-053", "Ultra Rare",   2002, None),
        ],
    },
    "sports_basketball": {
        "set": {"name": "Prizm Basketball", "code": "PRIZM", "tcg": TcgEnum.sports,
                "release_date": date(2023, 11, 1), "total": 300},
        "cards": [
            ("Victor Wembanyama RC",   "136/300", "Rookie",  2023, None),
            ("LeBron James",           "1/300",   "Base",    2023, None),
            ("Stephen Curry",          "23/300",  "Base",    2023, None),
            ("Luka Doncic",            "33/300",  "Base",    2023, None),
            ("Jayson Tatum",           "12/300",  "Base",    2023, None),
        ],
    },
    "sports_baseball": {
        "set": {"name": "Topps Chrome", "code": "TC", "tcg": TcgEnum.sports,
                "release_date": date(2024, 8, 1), "total": 200},
        "cards": [
            ("Shohei Ohtani",          "17/200",  "Base",    2024, None),
            ("Aaron Judge",            "1/200",   "Base",    2024, None),
            ("Mookie Betts",           "50/200",  "Base",    2024, None),
            ("Ronald Acuña Jr.",       "100/200", "Base",    2024, None),
            ("Paul Skenes RC",         "150/200", "Rookie",  2024, None),
        ],
    },
    "onepiece": {
        "set": {"name": "Romance Dawn", "code": "OP-01", "tcg": TcgEnum.onepiece,
                "release_date": date(2022, 12, 2), "total": 121},
        "cards": [
            ("Monkey D. Luffy",  "OP01-001", "Leader",       2022, None),
            ("Roronoa Zoro",     "OP01-025", "Super Rare",   2022, None),
            ("Nami",             "OP01-016", "Rare",         2022, None),
            ("Shanks",           "OP01-120", "Secret Rare",  2022, None),
        ],
    },
    "lorcana": {
        "set": {"name": "The First Chapter", "code": "TFC", "tcg": TcgEnum.lorcana,
                "release_date": date(2023, 8, 18), "total": 216},
        "cards": [
            ("Elsa - Snow Queen",         "42/204", "Legendary", 2023, None),
            ("Mickey Mouse - Brave Tiny", "1/204",  "Super Rare", 2023, None),
            ("Maleficent - Sorceress",    "100/204", "Rare",     2023, None),
            ("Stitch - Carefree Surfer",  "180/204", "Common",   2023, None),
        ],
    },
}

# Grade distribution per archetype (avg grade × portfolio size)
PROFILES: dict[str, dict] = {
    "vintage_pokemon":    {"avg_grade": 8.5, "size": 12, "house_mix": [("psa", 0.7), ("loupe", 0.3)]},
    "modern_pokemon":     {"avg_grade": 9.2, "size": 18, "house_mix": [("psa", 0.5), ("loupe", 0.5)]},
    "magic_reserved":     {"avg_grade": 7.8, "size": 8,  "house_mix": [("bgs", 0.6), ("psa", 0.4)]},
    "yugioh_meta":        {"avg_grade": 8.8, "size": 15, "house_mix": [("psa", 0.6), ("cgc", 0.4)]},
    "sports_basketball":  {"avg_grade": 9.0, "size": 14, "house_mix": [("psa", 0.8), ("bgs", 0.2)]},
    "sports_baseball":    {"avg_grade": 8.7, "size": 16, "house_mix": [("psa", 0.7), ("sgc", 0.3)]},
    "onepiece":           {"avg_grade": 9.3, "size": 10, "house_mix": [("loupe", 0.7), ("cgc", 0.3)]},
    "lorcana":            {"avg_grade": 9.1, "size": 9,  "house_mix": [("loupe", 1.0)]},
    "mixed_high_end":     {"avg_grade": 9.5, "size": 25, "house_mix": [("psa", 0.6), ("bgs", 0.3), ("cgc", 0.1)]},
    "mixed_beginner":     {"avg_grade": 7.5, "size": 6,  "house_mix": [("loupe", 1.0)]},
}

# Base values per card name (rough realistic USD for PSA 10)
VALUES_USD: dict[str, Decimal] = {
    # Vintage Pokémon
    "Charizard": Decimal("8500"), "Blastoise": Decimal("950"), "Venusaur": Decimal("700"),
    "Pikachu": Decimal("125"), "Machamp": Decimal("180"), "Alakazam": Decimal("450"),
    # Modern Pokémon
    "Charizard VSTAR": Decimal("220"), "Mew VMAX": Decimal("180"), "Lugia VSTAR": Decimal("85"),
    "Giratina VSTAR": Decimal("65"), "Arceus VSTAR": Decimal("75"),
    # Magic
    "Black Lotus": Decimal("420000"), "Mox Sapphire": Decimal("85000"),
    "Ancestral Recall": Decimal("48000"), "Time Walk": Decimal("40000"), "Timetwister": Decimal("18000"),
    # Yu-Gi-Oh
    "Blue-Eyes White Dragon": Decimal("2200"), "Dark Magician": Decimal("1500"),
    "Exodia the Forbidden": Decimal("950"), "Red-Eyes Black Dragon": Decimal("850"),
    "Summoned Skull": Decimal("280"),
    # Basketball
    "Victor Wembanyama RC": Decimal("1800"), "LeBron James": Decimal("450"),
    "Stephen Curry": Decimal("220"), "Luka Doncic": Decimal("180"), "Jayson Tatum": Decimal("95"),
    # Baseball
    "Shohei Ohtani": Decimal("380"), "Aaron Judge": Decimal("220"), "Mookie Betts": Decimal("110"),
    "Ronald Acuña Jr.": Decimal("140"), "Paul Skenes RC": Decimal("280"),
    # One Piece
    "Monkey D. Luffy": Decimal("65"), "Roronoa Zoro": Decimal("85"),
    "Nami": Decimal("45"), "Shanks": Decimal("320"),
    # Lorcana
    "Elsa - Snow Queen": Decimal("180"), "Mickey Mouse - Brave Tiny": Decimal("220"),
    "Maleficent - Sorceress": Decimal("85"), "Stitch - Carefree Surfer": Decimal("25"),
}


def _grade_for(avg: float) -> Decimal:
    """Sample a grade clamped to [3.0, 10.0] centred at ``avg``."""
    g = max(3.0, min(10.0, random.gauss(avg, 1.0)))
    return Decimal(str(round(g * 2) / 2))  # nearest 0.5


def _value_for(card_name: str, grade: Decimal) -> Decimal:
    """Scale base PSA-10 value by grade (rough curve)."""
    base = VALUES_USD.get(card_name, Decimal("50"))
    multiplier = {
        Decimal("10.0"): Decimal("1.00"),
        Decimal("9.5"):  Decimal("0.55"),
        Decimal("9.0"):  Decimal("0.30"),
        Decimal("8.5"):  Decimal("0.18"),
        Decimal("8.0"):  Decimal("0.12"),
        Decimal("7.5"):  Decimal("0.08"),
        Decimal("7.0"):  Decimal("0.06"),
    }.get(grade, Decimal("0.04"))
    return (base * multiplier).quantize(Decimal("0.01"))


def _pick_house(mix: list[tuple[str, float]]) -> GradeHouseEnum:
    r = random.random()
    cum = 0.0
    for name, p in mix:
        cum += p
        if r <= cum:
            return GradeHouseEnum(name)
    return GradeHouseEnum(mix[-1][0])


async def _get_or_create_catalog(db) -> dict[str, list[Card]]:
    """Ensure every archetype's set + cards exist, return cards by archetype."""
    out: dict[str, list[Card]] = {}
    for archetype, spec in CATALOG.items():
        set_spec = spec["set"]
        existing_set = (
            await db.execute(select(CardSet).where(CardSet.code == set_spec["code"]))
        ).scalar_one_or_none()
        if existing_set is None:
            cset = CardSet(
                tcg=set_spec["tcg"],
                name=set_spec["name"],
                code=set_spec["code"],
                release_date=set_spec.get("release_date"),
                total_cards=set_spec.get("total"),
            )
            db.add(cset)
            await db.flush()
        else:
            cset = existing_set

        cards: list[Card] = []
        for name, number, rarity, year, image_url in spec["cards"]:
            existing_card = (
                await db.execute(
                    select(Card).where(Card.set_id == cset.id, Card.name == name, Card.number == number)
                )
            ).scalar_one_or_none()
            if existing_card is None:
                card = Card(
                    set_id=cset.id, tcg=set_spec["tcg"], name=name,
                    number=number, rarity=rarity, year=year, image_url=image_url,
                )
                db.add(card)
                await db.flush()
            else:
                card = existing_card
            cards.append(card)
        out[archetype] = cards
    await db.commit()
    return out


async def seed_user(db, *, email: str, name: str, archetype: str, catalog: dict[str, list[Card]]) -> User:
    """Create the user + portfolio if they don't already exist."""
    user = await get_by_email(db, email)
    if user is None:
        user = await create_with_password(
            db, email=email, password=DEFAULT_PASSWORD, display_name=name,
        )
        logger.info("Created user %s (%s)", email, archetype)
    else:
        logger.info("User %s already exists; skipping create", email)

    # Check if portfolio already seeded for idempotency.
    existing_grades = (
        await db.execute(select(GradedCard).where(GradedCard.user_id == user.id))
    ).scalars().all()
    if existing_grades:
        logger.info("  %d graded cards already exist for %s; skipping portfolio", len(existing_grades), email)
        return user

    profile = PROFILES[archetype]
    # "mixed" archetypes pull from every catalog; specific archetypes use only theirs.
    if archetype in catalog:
        cards = catalog[archetype]
    else:
        cards = [c for cs in catalog.values() for c in cs]
    portfolio_size = profile["size"]
    avg_grade = profile["avg_grade"]
    house_mix = profile["house_mix"]

    # Build portfolio with weighted random picks from the archetype's catalog.
    base_date = datetime.now(timezone.utc) - timedelta(days=180)
    for i in range(portfolio_size):
        card = random.choice(cards)
        grade = _grade_for(avg_grade)
        value = _value_for(card.name, grade)
        house = _pick_house(house_mix)
        graded_at = base_date + timedelta(days=random.randint(0, 180), hours=random.randint(0, 23))
        gc = GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=grade,
            house=house,
            estimated_value_usd=value,
            graded_at=graded_at,
            subgrades={
                "centering": float(grade) - random.uniform(0, 0.5),
                "corners":   float(grade) - random.uniform(0, 0.3),
                "edges":     float(grade) - random.uniform(0, 0.3),
                "surface":   float(grade) - random.uniform(0, 0.4),
            },
            notes=None,
        )
        db.add(gc)

    # Add a default collection ("My Vault") and link first 3 graded cards.
    coll = Collection(
        user_id=user.id, name="My Vault",
        description=f"{name}'s personal collection",
        color="#00F59B", is_public=False,
    )
    db.add(coll)
    await db.flush()
    # Re-fetch graded cards to link them.
    graded = (
        await db.execute(
            select(GradedCard).where(GradedCard.user_id == user.id).limit(3)
        )
    ).scalars().all()
    for gc in graded:
        db.add(CollectionItem(collection_id=coll.id, graded_card_id=gc.id))

    await db.commit()
    logger.info("  Seeded %d graded cards for %s", portfolio_size, email)
    return user


async def main() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        catalog = await _get_or_create_catalog(db)
        for spec in TEST_USERS:
            await seed_user(
                db,
                email=spec["email"],
                name=spec["name"],
                archetype=spec["archetype"],
                catalog=catalog,
            )

        total_users = (await db.execute(
            select(User).where(User.email.like("test+%@loupe.app"))
        )).scalars().all()
        total_grades = (await db.execute(select(GradedCard))).scalars().all()
        total_value = sum(
            (g.estimated_value_usd or Decimal("0")) for g in total_grades
        )
        print()
        print(f"✓ {len(total_users)} test users seeded")
        print(f"✓ {len(total_grades)} graded cards across all users")
        print(f"✓ Total portfolio value: ${total_value:,.2f} USD")
        print(f"✓ Login with any email above + password: {DEFAULT_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
