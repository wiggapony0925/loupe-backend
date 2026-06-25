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
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.enums import TcgEnum
from app.models.user import User
from app.platform.rate_limit import (
    catalog_read_limit,
    resolve_limit,
    search_live_limit,
)
from app.schemas.card import CardRead
from app.schemas.common import Pagination
from app.schemas.ownership import CardOwnership
from app.services.catalog import (
    canonical_card_service,
    card_catalog_service,
    card_resolver_service,
    card_search_service,
)
from app.services.collection import graded_card_service
from app.services.market import (
    grade_summary_service,
    listings_service,
    market_service,
    marketplace_prices_service,
    nearby_listings_service,
    trending_service,
    valuation_service,
)
from app.services.market import sold_comps_service as comps_service

router = APIRouter(prefix="/cards", tags=["cards"])


@router.get(
    "/search",
    summary="Live card search (public)",
    dependencies=[Depends(search_live_limit)],
)
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


@router.get(
    "/trending",
    summary="Trending cards (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_trending(
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|all)$"),
    limit: int = Query(24, ge=1, le=100),
) -> dict[str, Any]:
    """Mixed trending feed across the three live catalogs.

    Cached for 15 minutes. Falls back to a small hardcoded set of
    well-known cards if every upstream is unreachable, so the endpoint
    never returns a 5xx.
    """
    return await trending_service.get_trending(tcg=tcg, limit=limit)


@router.get(
    "",
    response_model=Pagination[CardRead],
    summary="Search cards (DB)",
    dependencies=[Depends(catalog_read_limit)],
)
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


class ResolveCardRequest(BaseModel):
    """Body for ``POST /v1/cards/resolve``.

    Provide any combination — first non-empty resolves wins (order:
    ``uuid`` → ``upstream_id`` → ``phash`` → ``query``).
    """

    upstream_id: str | None = Field(
        None,
        description="Composite catalog id like 'pokemontcg:base1-4' or 'psa:12345'.",
    )
    query: str | None = Field(
        None, max_length=200, description="Free-text (name + set + #number)."
    )
    phash: str | None = Field(
        None, max_length=128, description="Perceptual image hash from a scan."
    )
    uuid: str | None = Field(None, description="Existing local Card UUID.")
    tcg: str | None = Field(
        None,
        pattern="^(pokemon|magic|yugioh|onepiece|lorcana|sports|all)$",
        description="Optional tcg hint to narrow free-text matches.",
    )
    materialize: bool = Field(
        True,
        description=(
            "When true (default), upstream-only hits are persisted as local "
            "Card + CardExternalRef rows so the user can attach grades / "
            "collection items / alerts to a stable UUID."
        ),
    )


@router.post(
    "/resolve",
    summary="Resolve any card hint to a canonical Loupe id (public)",
    dependencies=[Depends(resolve_limit)],
)
async def resolve_card(
    payload: ResolveCardRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Single funnel: scan / search / deep-link / manual entry all land here.

    Returns ``{card_id, upstream_id, source, confidence, canonical}``
    where ``canonical`` is the same shape as
    ``GET /v1/cards/{id}/canonical`` — i.e. the user can render the
    full card from this one response. Returns 404 when no path resolves.
    """
    uuid_: uuid.UUID | None = None
    if payload.uuid:
        try:
            uuid_ = uuid.UUID(payload.uuid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid uuid") from exc

    resolved = await card_resolver_service.resolve(
        db,
        upstream_id=payload.upstream_id,
        query=payload.query,
        phash=payload.phash,
        uuid_=uuid_,
        tcg=payload.tcg,
        materialize=payload.materialize,
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No card matched")
    # Commit any newly-materialized rows so the next call sees them.
    try:
        await db.commit()
    except Exception:  # pragma: no cover - best effort
        await db.rollback()

    # Compose the full canonical document so the client has one response
    # to render everything from.
    canonical = None
    lookup_id = (
        str(resolved.card_id) if resolved.card_id is not None else resolved.upstream_id
    )
    if lookup_id:
        composed = await canonical_card_service.compose_canonical_card(lookup_id)
        if composed is not None:
            canonical = composed.model_dump(mode="json")

    return {
        "card_id": str(resolved.card_id) if resolved.card_id else None,
        "upstream_id": resolved.upstream_id,
        "source": resolved.source,
        "confidence": resolved.confidence,
        "canonical": canonical,
    }


@router.get(
    "/{card_id}/market",
    summary="Card market snapshot (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_market(card_id: str) -> dict[str, Any]:
    """Return the full per-house × per-grade market view for a card.

    Synthesizes the graded table deterministically from the live raw
    market price (seeded by card id) so the page is stable across
    refreshes — see :func:`app.services.market.market_service.build_market_for_card`.
    """
    result = await market_service.get_card_market(card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/ownership",
    response_model=CardOwnership,
    summary="My ownership of this card (signed-in)",
)
async def get_ownership(
    card_id: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> CardOwnership:
    """Per-user ownership context: every copy the signed-in user owns of this
    card, with grade/scan data and rolled-up cost basis, holding value, and
    unrealized P/L. Returns ``owned=false`` when they own none."""
    return await graded_card_service.get_card_ownership(db, user, card_id)


@router.get(
    "/{card_id}/prices",
    summary="Card price history (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_prices(
    card_id: str,
    range: str = Query("30d", pattern="^(7d|30d|90d|180d|1y|365d|all)$"),
    house: str = Query("raw"),
    grade: str | None = Query(None),
) -> dict[str, Any]:
    """Return a price history series for the given composite card id.

    Currently synthesizes a deterministic walk around the live market
    price (seeded by card id) so the chart is stable across refreshes.
    When ``house``/``grade`` are supplied the walk is scaled by the same
    drift × multiplier math that drives the per-house market table, so
    tap-a-grade-row → chart-filters-by-tier works without a second API.
    """
    result = await card_search_service.get_price_history(
        card_id, range_=range, house=house, grade=grade
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/listings",
    summary="Live for-sale listings (public)",
    dependencies=[Depends(catalog_read_limit)],
)
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


@router.get(
    "/{card_id}/nearby-listings",
    summary="Nearby Facebook Marketplace listings (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_nearby_listings(
    card_id: str,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_km: int = Query(40, ge=1, le=500),
    limit: int = Query(20, ge=1, le=50),
) -> dict[str, Any]:
    """Facebook Marketplace listings near the user for the viewed card.

    Powers the "Near You" carousel on the card-detail sheet. ``lat``/``lng``
    come from the device's location (foreground permission). Returns
    ``listings: []`` when Apify is unconfigured or has no matches, so the
    client always renders a clean empty state.
    """
    result = await nearby_listings_service.get_nearby_listings_for_card(
        card_id, lat=lat, lng=lng, radius_km=radius_km, limit=limit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/comps",
    summary="Recent sold comps (public)",
    dependencies=[Depends(catalog_read_limit)],
)
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


@router.get(
    "/{card_id}/grade-summary",
    summary="Per-grade price summary (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_grade_summary(
    card_id: str,
    window_days: int = Query(30, ge=1, le=180),
) -> dict[str, Any]:
    """Pivot recent sold comps into a per-grade summary for the card.

    Powers the price-by-grade pill row on the scan/detail sheet:
    UNGRADED $X · PSA 10 $Y · BGS 10 $Z. Returns ``grades: []`` when
    no comps are available so the client always renders.
    """
    result = await grade_summary_service.get_grade_summary_for_card(
        card_id, window_days=window_days
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/valuation",
    summary="Loupe Value — equilibrium fair value + per-grade ladder (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_valuation(card_id: str) -> dict[str, Any]:
    """One equilibrium "fair value" for the raw card (blended from sold comps,
    live listings, and catalog market), the transparent input signals, and the
    per-grade price ladder (PSA 10 / BGS 9.5 / …) — in a single round-trip."""
    result = await valuation_service.get_valuation(card_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/marketplace-prices",
    summary="Lowest active price per marketplace (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_marketplace_prices(
    card_id: str,
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    """Lowest live listing per provider + deep-link search URL.

    Powers the marketplace chip row on the card-detail sheet
    (eBay $X · TCGplayer $Y · ...). Empty ``providers`` is rendered as
    "no live listings yet" by the client.
    """
    result = await marketplace_prices_service.get_marketplace_prices_for_card(
        card_id, limit=limit
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return result


@router.get(
    "/{card_id}/canonical",
    summary="Canonical unified card (public)",
    dependencies=[Depends(catalog_read_limit)],
)
async def get_canonical(card_id: str) -> dict[str, Any]:
    """Return the single "holy grail" :class:`CanonicalCard` document.

    Composes catalog identity, set, images, attributes, multi-source
    pricing, population, listings, comps, and (for ``psa:<cert>`` ids)
    a verified cert into one object the frontend can render directly.
    Section provenance is included so the UI can flag real vs
    synthesized data.
    """
    card = await canonical_card_service.compose_canonical_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card.model_dump(mode="json")


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
