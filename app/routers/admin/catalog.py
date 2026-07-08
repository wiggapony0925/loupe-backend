"""Admin catalog-coverage analytics + local-mirror ops (`/v1/admin/catalog`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.catalog import CatalogCoverage
from app.services.admin import catalog_service
from app.services.catalog import pokemon_mirror_service

router = APIRouter(prefix="/catalog", tags=["admin-catalog"])


@router.get("", response_model=CatalogCoverage, summary="Catalog coverage by game")
async def get_catalog(db: AsyncSession = Depends(get_db)) -> CatalogCoverage:
    return await catalog_service.summary(db)


@router.get("/mirror", summary="Local catalog mirror status")
async def get_mirror_status() -> dict[str, Any]:
    """Row counts + price freshness for the Pokémon mirror."""
    return await pokemon_mirror_service.mirror_status()


@router.post("/mirror/sync", summary="Sync the Pokémon mirror from bulk data")
async def sync_mirror(
    force: bool = Query(False, description="Re-sync sets that look complete."),
    max_sets: int = Query(
        40,
        ge=0,
        le=500,
        description=(
            "Sets to sync this call (newest-first); 0 = all remaining. "
            "Chunked so a call stays inside the request timeout — repeat "
            "until `sets_synced` comes back 0."
        ),
    ),
) -> dict[str, Any]:
    stats = await pokemon_mirror_service.sync_pokemon_from_dump(
        force=force, max_sets=max_sets or None
    )
    return {**stats, "status": await pokemon_mirror_service.mirror_status()}


@router.post("/mirror/sync-tcg", summary="Sync the Magic / Yu-Gi-Oh mirror")
async def sync_mirror_tcg(
    tcg: str = Query(..., pattern="^(magic|yugioh)$"),
    max_cards: int = Query(
        0,
        ge=0,
        description="Cap cards this call (0 = the full catalog). Both fit one call.",
    ),
) -> dict[str, Any]:
    """Populate/refresh the Magic (Scryfall bulk) or Yu-Gi-Oh (YGOPRODeck dump)
    mirror. Both catalogs ship prices, so no separate price-refresh is needed."""
    cap = max_cards or None
    if tcg == "magic":
        stats = await pokemon_mirror_service.sync_magic_from_bulk(max_cards=cap)
    else:
        stats = await pokemon_mirror_service.sync_yugioh_from_dump(max_cards=cap)
    return {**stats, "ready": await pokemon_mirror_service.mirror_ready(tcg)}


@router.post("/mirror/refresh-prices", summary="Refresh embedded prices per set")
async def refresh_mirror_prices(
    sets: int = Query(10, ge=1, le=60, description="Stale sets to refresh now."),
) -> dict[str, Any]:
    """Walk the stalest sets and pull fresh tcgplayer/cardmarket blocks from
    the live API (inline, so the caller sees real progress)."""
    set_ids = await pokemon_mirror_service.stale_price_set_ids(limit=sets)
    refreshed: dict[str, int] = {}
    for sid in set_ids:
        refreshed[sid] = await pokemon_mirror_service.refresh_set_prices(sid)
    return {
        "refreshed": refreshed,
        "status": await pokemon_mirror_service.mirror_status(),
    }


__all__ = ["router"]
