"""Catalog-coverage analytics — how much data backs each game, scanner-readiness
(perceptual-hash coverage), and where prices come from. All read-only."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum
from app.models.price import PriceSnapshot
from app.schemas.catalog import CatalogCoverage, GameCoverage

# Display names for the games the catalog knows about.
_LABELS: dict[str, str] = {
    "pokemon": "Pokémon",
    "magic": "Magic",
    "yugioh": "Yu-Gi-Oh!",
    "onepiece": "One Piece",
    "lorcana": "Lorcana",
    "sports": "Sports",
}


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar_one())


async def summary(db: AsyncSession) -> CatalogCoverage:
    games: list[GameCoverage] = []
    total_cards = total_sets = total_phash = 0

    for tcg in TcgEnum:
        sets = await _count(
            db, select(func.count()).select_from(CardSet).where(CardSet.tcg == tcg)
        )
        cards = await _count(
            db, select(func.count()).select_from(Card).where(Card.tcg == tcg)
        )
        phash = await _count(
            db,
            select(func.count())
            .select_from(Card)
            .where(Card.tcg == tcg, Card.image_phash.is_not(None)),
        )
        games.append(
            GameCoverage(
                tcg=tcg.value,
                label=_LABELS.get(tcg.value, tcg.value.title()),
                sets=sets,
                cards=cards,
                phash_cards=phash,
                phash_pct=round(phash / cards, 4) if cards else 0.0,
                backed=cards > 0,
            )
        )
        total_cards += cards
        total_sets += sets
        total_phash += phash

    price_snapshots = await _count(db, select(func.count()).select_from(PriceSnapshot))
    by_source_rows = (
        await db.execute(
            select(PriceSnapshot.source, func.count()).group_by(PriceSnapshot.source)
        )
    ).all()
    price_by_source = {
        (src.value if hasattr(src, "value") else str(src)): int(n)
        for src, n in by_source_rows
    }

    # Most-backed games first; empty (scaffolded) games sink to the bottom.
    games.sort(key=lambda g: g.cards, reverse=True)

    return CatalogCoverage(
        total_cards=total_cards,
        total_sets=total_sets,
        phash_coverage_pct=round(total_phash / total_cards, 4) if total_cards else 0.0,
        price_snapshots=price_snapshots,
        price_by_source=price_by_source,
        games=games,
    )


__all__ = ["summary"]
