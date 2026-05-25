"""Seed top sealed Pokémon SKUs into ``sealed_products``.

Idempotent — keyed by ``(upstream_source='manual', upstream_id=slug)``
so re-running the script just upserts. The catalog here is hand-curated
to cover the SKUs collectors actually care about (modern WOTC reprints
+ recent ETBs + tournament-grade booster boxes) so the search UI works
on day one without depending on the eBay / TCGplayer ingestion track.

Usage:
    python -m scripts.seed_sealed_products
    DATABASE_URL=... python -m scripts.seed_sealed_products
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedProduct
from app.utils.logger import get_logger

logger = get_logger("scripts.seed_sealed")


@dataclass(frozen=True)
class Seed:
    slug: str  # upstream_id under source='manual'
    name: str
    set_name: str | None
    product_type: SealedProductTypeEnum
    msrp_usd: Decimal | None
    release_date: date | None


# Hand-curated top sealed Pokémon SKUs (booster boxes + ETBs + premium).
# Sources: TCGplayer "most watched" + r/PokeInvesting top holdings as of
# early 2026. Update by appending — never reorder (slug → id stability).
SEEDS: list[Seed] = [
    # ── Booster Boxes (Scarlet & Violet era) ────────────────────────────
    Seed(
        "sv-151-booster-box",
        "Scarlet & Violet — 151 Booster Box",
        "Scarlet & Violet — 151",
        SealedProductTypeEnum.booster_box,
        Decimal("161.64"),
        date(2023, 9, 22),
    ),
    Seed(
        "sv-paldean-fates-booster-box",
        "Scarlet & Violet — Paldean Fates Booster Bundle Box",
        "Paldean Fates",
        SealedProductTypeEnum.booster_box,
        Decimal("149.94"),
        date(2024, 1, 26),
    ),
    Seed(
        "sv-temporal-forces-booster-box",
        "Scarlet & Violet — Temporal Forces Booster Box",
        "Temporal Forces",
        SealedProductTypeEnum.booster_box,
        Decimal("161.64"),
        date(2024, 3, 22),
    ),
    Seed(
        "sv-twilight-masquerade-booster-box",
        "Scarlet & Violet — Twilight Masquerade Booster Box",
        "Twilight Masquerade",
        SealedProductTypeEnum.booster_box,
        Decimal("161.64"),
        date(2024, 5, 24),
    ),
    Seed(
        "sv-shrouded-fable-booster-box",
        "Scarlet & Violet — Shrouded Fable Booster Box",
        "Shrouded Fable",
        SealedProductTypeEnum.booster_box,
        Decimal("149.94"),
        date(2024, 8, 2),
    ),
    Seed(
        "sv-stellar-crown-booster-box",
        "Scarlet & Violet — Stellar Crown Booster Box",
        "Stellar Crown",
        SealedProductTypeEnum.booster_box,
        Decimal("161.64"),
        date(2024, 9, 13),
    ),
    Seed(
        "sv-surging-sparks-booster-box",
        "Scarlet & Violet — Surging Sparks Booster Box",
        "Surging Sparks",
        SealedProductTypeEnum.booster_box,
        Decimal("161.64"),
        date(2024, 11, 8),
    ),
    Seed(
        "sv-prismatic-evolutions-booster-bundle-box",
        "Scarlet & Violet — Prismatic Evolutions Booster Bundle Box",
        "Prismatic Evolutions",
        SealedProductTypeEnum.booster_box,
        Decimal("149.94"),
        date(2025, 1, 17),
    ),
    # ── Booster Boxes (Sword & Shield era classics) ─────────────────────
    Seed(
        "ssh-brilliant-stars-booster-box",
        "Sword & Shield — Brilliant Stars Booster Box",
        "Brilliant Stars",
        SealedProductTypeEnum.booster_box,
        Decimal("143.64"),
        date(2022, 2, 25),
    ),
    Seed(
        "ssh-evolving-skies-booster-box",
        "Sword & Shield — Evolving Skies Booster Box",
        "Evolving Skies",
        SealedProductTypeEnum.booster_box,
        Decimal("143.64"),
        date(2021, 8, 27),
    ),
    Seed(
        "ssh-lost-origin-booster-box",
        "Sword & Shield — Lost Origin Booster Box",
        "Lost Origin",
        SealedProductTypeEnum.booster_box,
        Decimal("143.64"),
        date(2022, 9, 9),
    ),
    Seed(
        "ssh-silver-tempest-booster-box",
        "Sword & Shield — Silver Tempest Booster Box",
        "Silver Tempest",
        SealedProductTypeEnum.booster_box,
        Decimal("143.64"),
        date(2022, 11, 11),
    ),
    # ── ETBs ────────────────────────────────────────────────────────────
    Seed(
        "sv-151-etb",
        "Scarlet & Violet — 151 Elite Trainer Box",
        "Scarlet & Violet — 151",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2023, 9, 22),
    ),
    Seed(
        "sv-paldean-fates-etb",
        "Scarlet & Violet — Paldean Fates Elite Trainer Box",
        "Paldean Fates",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 1, 26),
    ),
    Seed(
        "sv-temporal-forces-etb",
        "Scarlet & Violet — Temporal Forces Elite Trainer Box",
        "Temporal Forces",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 3, 22),
    ),
    Seed(
        "sv-twilight-masquerade-etb",
        "Scarlet & Violet — Twilight Masquerade Elite Trainer Box",
        "Twilight Masquerade",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 5, 24),
    ),
    Seed(
        "sv-shrouded-fable-etb",
        "Scarlet & Violet — Shrouded Fable Elite Trainer Box",
        "Shrouded Fable",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 8, 2),
    ),
    Seed(
        "sv-stellar-crown-etb",
        "Scarlet & Violet — Stellar Crown Elite Trainer Box",
        "Stellar Crown",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 9, 13),
    ),
    Seed(
        "sv-surging-sparks-etb",
        "Scarlet & Violet — Surging Sparks Elite Trainer Box",
        "Surging Sparks",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2024, 11, 8),
    ),
    Seed(
        "ssh-evolving-skies-etb",
        "Sword & Shield — Evolving Skies Elite Trainer Box",
        "Evolving Skies",
        SealedProductTypeEnum.etb,
        Decimal("44.99"),
        date(2021, 8, 27),
    ),
    Seed(
        "ssh-brilliant-stars-etb",
        "Sword & Shield — Brilliant Stars Elite Trainer Box",
        "Brilliant Stars",
        SealedProductTypeEnum.etb,
        Decimal("44.99"),
        date(2022, 2, 25),
    ),
    Seed(
        "ssh-crown-zenith-etb",
        "Sword & Shield — Crown Zenith Elite Trainer Box",
        "Crown Zenith",
        SealedProductTypeEnum.etb,
        Decimal("49.99"),
        date(2023, 1, 20),
    ),
    # ── Premium / Collection boxes ──────────────────────────────────────
    Seed(
        "sv-151-ultra-premium-collection",
        "Scarlet & Violet — 151 Ultra-Premium Collection",
        "Scarlet & Violet — 151",
        SealedProductTypeEnum.premium_collection,
        Decimal("119.99"),
        date(2023, 10, 6),
    ),
    Seed(
        "ssh-crown-zenith-upc",
        "Sword & Shield — Crown Zenith Ultra-Premium Collection",
        "Crown Zenith",
        SealedProductTypeEnum.premium_collection,
        Decimal("119.99"),
        date(2023, 1, 20),
    ),
    Seed(
        "sv-prismatic-evolutions-upc",
        "Scarlet & Violet — Prismatic Evolutions Super-Premium Collection",
        "Prismatic Evolutions",
        SealedProductTypeEnum.premium_collection,
        Decimal("99.99"),
        date(2025, 2, 21),
    ),
    # ── Tins / Blisters ─────────────────────────────────────────────────
    Seed(
        "sv-charizard-ex-premium-collection",
        "Scarlet & Violet — Charizard ex Premium Collection",
        "Scarlet & Violet Promos",
        SealedProductTypeEnum.tin,
        Decimal("39.99"),
        date(2023, 11, 17),
    ),
    Seed(
        "sv-mew-ex-collection",
        "Scarlet & Violet — Mew ex Premium Collection",
        "Scarlet & Violet Promos",
        SealedProductTypeEnum.tin,
        Decimal("39.99"),
        date(2024, 4, 5),
    ),
    Seed(
        "sv-pikachu-ex-blister",
        "Scarlet & Violet — Pikachu ex Box",
        "Scarlet & Violet Promos",
        SealedProductTypeEnum.blister,
        Decimal("19.99"),
        date(2024, 2, 16),
    ),
    # ── Bundles / Booster bundles ───────────────────────────────────────
    Seed(
        "sv-151-booster-bundle",
        "Scarlet & Violet — 151 Booster Bundle (6 packs)",
        "Scarlet & Violet — 151",
        SealedProductTypeEnum.bundle,
        Decimal("26.94"),
        date(2023, 9, 22),
    ),
    Seed(
        "sv-prismatic-evolutions-booster-bundle",
        "Scarlet & Violet — Prismatic Evolutions Booster Bundle (6 packs)",
        "Prismatic Evolutions",
        SealedProductTypeEnum.bundle,
        Decimal("26.94"),
        date(2025, 1, 17),
    ),
    Seed(
        "sv-surging-sparks-booster-bundle",
        "Scarlet & Violet — Surging Sparks Booster Bundle (6 packs)",
        "Surging Sparks",
        SealedProductTypeEnum.bundle,
        Decimal("26.94"),
        date(2024, 11, 8),
    ),
]


async def _seed_one(session, seed: Seed) -> bool:
    """Upsert one seed by ``(upstream_source='manual', slug)``.

    Returns True if a row was created, False if it already existed.
    """
    stmt = select(SealedProduct).where(
        SealedProduct.upstream_source == "manual",
        SealedProduct.upstream_id == seed.slug,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        # Keep mutable fields current so future tweaks to MSRP / image
        # propagate on re-run without an alembic migration.
        existing.name = seed.name
        existing.set_name = seed.set_name
        existing.product_type = seed.product_type
        existing.msrp_usd = seed.msrp_usd
        existing.release_date = seed.release_date
        return False
    session.add(
        SealedProduct(
            tcg=TcgEnum.pokemon,
            product_type=seed.product_type,
            name=seed.name,
            set_name=seed.set_name,
            msrp_usd=seed.msrp_usd,
            release_date=seed.release_date,
            upstream_source="manual",
            upstream_id=seed.slug,
        )
    )
    return True


async def main() -> None:
    Session = get_sessionmaker()
    created = 0
    updated = 0
    async with Session() as session:
        for seed in SEEDS:
            was_new = await _seed_one(session, seed)
            if was_new:
                created += 1
            else:
                updated += 1
        await session.commit()
    logger.info(
        "sealed seed complete",
        extra={
            "seeded_created": created,
            "seeded_updated": updated,
            "seeded_total": len(SEEDS),
        },
    )
    print(
        f"Seeded sealed products — created={created} updated={updated} total={len(SEEDS)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
