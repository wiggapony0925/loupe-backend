"""Periodic catalog-sync worker: refresh card sets/cards from upstream TCG APIs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.clients import pokemon_tcg, scryfall, ygoprodeck
from app.db import get_sessionmaker
from app.models.card import CardSet
from app.models.enums import TcgEnum
from app.utils.logger import get_logger

logger = get_logger("workers.catalog")


async def _upsert_set(
    db, tcg: TcgEnum, name: str, code: str | None, image_url: str | None
) -> None:
    existing = (
        await db.execute(
            select(CardSet).where(CardSet.tcg == tcg, CardSet.name == name)
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(CardSet(tcg=tcg, name=name, code=code, image_url=image_url))
        return
    if code and existing.code != code:
        existing.code = code
    if image_url and existing.image_url != image_url:
        existing.image_url = image_url


async def catalog_sync(ctx: dict[str, Any] | None = None) -> dict[str, int]:
    """Refresh ``card_sets`` for every supported TCG. Returns counts per TCG."""
    counts: dict[str, int] = {}
    sm = get_sessionmaker()
    async with sm() as db:
        # Pokémon
        try:
            pokemon_sets = await pokemon_tcg.list_sets()
        except Exception as exc:
            logger.warning("Pokémon TCG sync failed: %s", exc)
            pokemon_sets = []
        for entry in pokemon_sets:
            await _upsert_set(
                db,
                TcgEnum.pokemon,
                name=str(entry.get("name") or "Unknown"),
                code=entry.get("id") or entry.get("code"),
                image_url=(entry.get("images") or {}).get("logo")
                if isinstance(entry.get("images"), dict)
                else entry.get("image_url"),
            )
        counts["pokemon"] = len(pokemon_sets)

        # Magic: the Gathering (via Scryfall)
        try:
            magic_sets = await scryfall.list_sets()
        except Exception as exc:
            logger.warning("Scryfall sync failed: %s", exc)
            magic_sets = []
        for entry in magic_sets:
            await _upsert_set(
                db,
                TcgEnum.magic,
                name=str(entry.get("name") or "Unknown"),
                code=entry.get("code"),
                image_url=entry.get("icon_svg_uri"),
            )
        counts["magic"] = len(magic_sets)

        # Yu-Gi-Oh
        try:
            yugioh_sets = await ygoprodeck.list_sets()
        except Exception as exc:
            logger.warning("YGOPRODeck sync failed: %s", exc)
            yugioh_sets = []
        for entry in yugioh_sets:
            await _upsert_set(
                db,
                TcgEnum.yugioh,
                name=str(entry.get("set_name") or entry.get("name") or "Unknown"),
                code=entry.get("set_code"),
                image_url=None,
            )
        counts["yugioh"] = len(yugioh_sets)

        await db.commit()

    logger.info("catalog_sync complete: %s", counts)
    return counts


__all__ = ["catalog_sync"]
