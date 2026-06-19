"""Public web storefront API — ``/v1/public/*``.

A thin, no-auth, cacheable surface for the web client. This is where the
**heavy lifting the browser used to do** now happens server-side: filtering,
sorting, pagination, and faceting of search results, plus the trending
variants (by value, price ceiling). It reuses the existing catalog/market
services — no new upstreams, no DB writes, no migrations.

Keeping this as its own namespace (rather than overloading ``/v1/cards``)
gives the web a stable, decoupled contract we can cache and rate-limit
independently of the mobile app's catalog endpoints.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.platform.rate_limit import catalog_read_limit, search_live_limit
from app.services.catalog import card_search_service, catalog_browse_service
from app.services.market import trending_service

router = APIRouter(prefix="/public", tags=["public"])

#: tcg values the trending feed understands (others collapse to "all").
_TRENDING_TCGS = {"pokemon", "magic", "yugioh", "all"}


def _market_amount(card: dict[str, Any]) -> float | None:
    """Best-available numeric price for a card dict (market, else low)."""
    pricing = card.get("pricing_summary") or {}
    chosen = pricing.get("market") or pricing.get("low")
    if isinstance(chosen, dict) and chosen.get("amount") is not None:
        try:
            return float(chosen["amount"])
        except (TypeError, ValueError):
            return None
    return None


def _apply_sort(cards: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "price_asc":
        return sorted(cards, key=lambda c: (_market_amount(c) is None, _market_amount(c) or 0.0))
    if sort == "price_desc":
        return sorted(cards, key=lambda c: _market_amount(c) or 0.0, reverse=True)
    if sort == "name":
        return sorted(cards, key=lambda c: (c.get("name") or "").lower())
    return cards  # "best" / "trending" → keep upstream order


@router.get(
    "/search",
    summary="Public storefront search (server-side filter / sort / paginate / facets)",
    dependencies=[Depends(search_live_limit)],
)
async def public_search(
    q: str = Query("", max_length=120),
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|onepiece|lorcana|sports|all)$"),
    rarity: str | None = Query(None, max_length=80),
    set_name: str | None = Query(None, alias="set", max_length=120),
    sort: str = Query("best", pattern="^(best|price_asc|price_desc|name)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=60),
) -> dict[str, Any]:
    """Search (or, with no query, browse trending) with all derivation done here.

    Returns the paginated slice plus ``total`` and ``facets`` so the client
    renders directly — no client-side filtering/sorting/pagination.
    """
    if q.strip():
        # limit=20 matches the warm upstream cache key used by /v1/cards/search;
        # larger limits are a separate (often cold) cache entry.
        body = await card_search_service.search_cards(q=q, tcg=tcg, limit=20)
        cards = list(body.get("results") or [])
        source = body.get("source")
    else:
        body = await trending_service.get_trending(
            tcg=tcg if tcg in _TRENDING_TCGS else "all", limit=100
        )
        cards = list(body.get("cards") or [])
        source = body.get("source")

    # Facets reflect the full (pre-filter) result set.
    rarities = sorted({c.get("rarity") for c in cards if c.get("rarity")})
    sets = sorted({c.get("set_name") for c in cards if c.get("set_name")})

    filtered = [
        c
        for c in cards
        if (rarity is None or c.get("rarity") == rarity)
        and (set_name is None or c.get("set_name") == set_name)
    ]
    ordered = _apply_sort(filtered, sort)
    total = len(ordered)
    start = (page - 1) * page_size
    page_items = ordered[start : start + page_size]

    return {
        "results": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": {"rarities": rarities, "sets": sets},
        "source": source,
    }


@router.get(
    "/browse",
    summary="Browse a whole game catalog (paginated thousands of cards)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_browse(
    game: str = Query("pokemon", pattern="^(pokemon|magic|yugioh|lorcana|onepiece|digimon|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=50),
    sort: str = Query("name", pattern="^(name|newest|price_asc|price_desc)$"),
) -> dict[str, Any]:
    """Page through an entire game's upstream catalog (real ``total`` for paging),
    sorted server-side. Unsupported games return an empty page rather than an error."""
    return await catalog_browse_service.browse_catalog(game, page, page_size, sort=sort)


@router.get(
    "/trending",
    summary="Public trending (server-side sort / price ceiling)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_trending(
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|all)$"),
    sort: str = Query("trending", pattern="^(trending|value)$"),
    max_price: float | None = Query(None, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Trending feed with the cut applied server-side (e.g. ``sort=value`` for
    "most valuable", ``max_price=5`` for "steals under $5")."""
    body = await trending_service.get_trending(tcg=tcg, limit=100)
    cards = list(body.get("cards") or [])

    if max_price is not None:
        cards = [c for c in cards if (_market_amount(c) or 0.0) <= max_price]
    if sort == "value":
        cards = _apply_sort(cards, "price_desc")

    return {
        "cards": cards[:limit],
        "total": len(cards),
        "source": body.get("source"),
        "updated_at": body.get("updated_at"),
    }


__all__ = ["router"]
