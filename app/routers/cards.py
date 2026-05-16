"""Card catalog endpoints.

Public read-only endpoints (no auth required):

* ``GET /cards/search`` — live upstream search proxy (unified shape).
* ``GET /cards/{card_id}`` — single-card lookup; accepts either a local UUID
  (DB-backed legacy lookup) or a composite ``<source>:<upstream_id>`` ID.
* ``GET /cards`` — legacy paginated DB search (kept for the existing
  local-catalog flow used by tests and the catalog-sync worker).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.enums import TcgEnum
from app.schemas.card import CardRead
from app.schemas.common import Pagination
from app.services import (
    card_catalog_service,
    card_search_service,
    comps_service,
    listings_service,
    market_service,
    trending_service,
)

router = APIRouter(prefix="/cards", tags=["cards"])


@router.get("/search", summary="Live card search (public)")
async def search_live(
    q: str = Query("", max_length=120),
    tcg: str = Query(
        "all",
        pattern="^(pokemon|magic|yugioh|onepiece|lorcana|sports|all)$",
    ),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    """Live search against Scryfall / Pokémon TCG / YGOPRODeck.

    Always returns 200; upstream errors are surfaced via an ``error`` field
    on an otherwise-empty envelope so the mobile client can degrade
    gracefully.
    """
    return await card_search_service.search_cards(q=q, tcg=tcg, limit=limit)


@router.get("/trending", summary="Trending cards (public)")
async def get_trending(
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|all)$"),
    limit: int = Query(24, ge=1, le=48),
) -> dict[str, Any]:
    """Mixed trending feed across the three live catalogs.

    Cached for 15 minutes. Falls back to a small hardcoded set of
    well-known cards if every upstream is unreachable, so the endpoint
    never returns a 5xx.
    """
    return await trending_service.get_trending(tcg=tcg, limit=limit)


@router.get("", response_model=Pagination[CardRead], summary="Search cards (DB)")
async def search(
    q: str | None = Query(None, max_length=120),
    tcg: TcgEnum | None = None,
    set_code: str | None = Query(None, max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> Pagination[CardRead]:
    """Legacy DB-backed paginated catalog search."""
    rows, total = await card_catalog_service.search_cards(
        db, q=q, tcg=tcg, set_code=set_code, page=page, page_size=page_size
    )
    return Pagination[CardRead](
        items=[CardRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{card_id}/market", summary="Card market snapshot (public)")
async def get_market(card_id: str) -> dict[str, Any]:
    """Return the full per-house × per-grade market view for a card.

    Synthesizes the graded table deterministically from the live raw
    market price (seeded by card id) so the page is stable across
    refreshes — see :func:`app.services.market_service.build_market_for_card`.
    """
    result = await market_service.get_card_market(card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get("/{card_id}/prices", summary="Card price history (public)")
async def get_prices(
    card_id: str,
    range: str = Query("30d", pattern="^(7d|30d|90d|180d|1y|365d)$"),
    house: str = Query("raw"),
    grade: str | None = Query(None),
) -> dict[str, Any]:
    """Return a price history series for the given composite card id.

    Currently synthesizes a deterministic walk around the live market
    price (seeded by card id) so the chart is stable across refreshes.
    """
    result = await card_search_service.get_price_history(card_id, range_=range)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    # ``house``/``grade`` are accepted (and reserved) for forward-compat;
    # current synthesizer treats every request as raw/ungraded.
    result["house"] = house
    if grade:
        result["grade"] = grade
    return result


@router.get("/{card_id}/listings", summary="Live for-sale listings (public)")
async def get_listings(
    card_id: str,
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    """Live listings fanned out across configured providers (eBay, ...).

    Returns ``listings: []`` when no provider is configured, so the
    client always renders successfully.
    """
    result = await listings_service.get_listings_for_card(card_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get("/{card_id}/comps", summary="Recent sold comps (public)")
async def get_comps(
    card_id: str,
    days: int = Query(90, ge=1, le=365),
    grade: str | None = Query(None, max_length=8),
    house: str | None = Query(None, max_length=8),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    """Recent sold comps fanned out across configured providers."""
    result = await comps_service.get_comps_for_card(
        card_id, days=days, grade=grade, house=house, limit=limit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get("/{card_id}", summary="Get one card (public)")
async def get_one(
    card_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Lookup by composite ``<source>:<upstream_id>`` or local UUID."""
    if ":" in card_id:
        result = await card_search_service.get_card(card_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Card not found")
        return result

    try:
        as_uuid = uuid.UUID(card_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid card id") from exc

    row = await card_catalog_service.get_card(db, as_uuid)
    if row is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return {
        "id": str(row.id),
        "name": row.name,
        "tcg": row.tcg.value if hasattr(row.tcg, "value") else str(row.tcg),
        "set_name": None,
        "set_code": None,
        "number": row.number,
        "rarity": row.rarity,
        "image_url": row.image_url,
        "year": row.year,
        "source": "loupe-db",
    }


__all__ = ["router"]
