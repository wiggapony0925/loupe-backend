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

from fastapi import APIRouter, Depends, Query, Response

from app.platform.rate_limit import catalog_read_limit, search_live_limit
from app.services.catalog import card_search_service, catalog_browse_service
from app.services.market import trending_service

router = APIRouter(prefix="/public", tags=["public"])

# Catalog identity (names, sets, art, rarity) is effectively immutable; only
# pricing drifts. So we let the browser/CDN serve a cached copy instantly and
# revalidate in the background — the client then refreshes prices. This is what
# makes cards "never reload" on navigation/refresh.
_CATALOG_CACHE = "public, max-age=120, stale-while-revalidate=86400"


def _cache(response: Response) -> None:
    response.headers["Cache-Control"] = _CATALOG_CACHE


#: tcg values the trending feed understands (others collapse to "all").
_TRENDING_TCGS = {"pokemon", "magic", "yugioh", "all"}


def _market_amount(card: dict[str, Any]) -> float | None:
    """Best-available numeric price for a card dict, or None if it has none.

    Falls through market → high → mid → low so a card with only one price band
    still counts as "priced" (and isn't dropped from the shopping rails)."""
    pricing = card.get("pricing_summary") or {}
    for key in ("market", "high", "mid", "low"):
        chosen = pricing.get(key)
        if isinstance(chosen, dict) and chosen.get("amount") is not None:
            try:
                return float(chosen["amount"])
            except (TypeError, ValueError):
                continue
    return None


def _apply_sort(cards: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "price_asc":
        return sorted(
            cards, key=lambda c: (_market_amount(c) is None, _market_amount(c) or 0.0)
        )
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
    response: Response,
    q: str = Query("", max_length=120),
    tcg: str = Query(
        "all", pattern="^(pokemon|magic|yugioh|onepiece|lorcana|sports|all)$"
    ),
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
    _cache(response)
    if q.strip():
        # The typeahead (small page_size) only needs a cheap top-N, so it reuses
        # the warm limit=20 upstream cache key. The full results page (larger
        # page_size) fetches a deep set so users can page through big result
        # counts -- e.g. 200+ Mewtwo printings -- instead of a hard 20-row cap.
        fetch_limit = 20 if page_size <= 12 else 200
        body = await card_search_service.search_cards(q=q, tcg=tcg, limit=fetch_limit)
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
    response: Response,
    game: str = Query(
        "pokemon", pattern="^(pokemon|magic|yugioh|lorcana|onepiece|digimon|all)$"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=50),
    sort: str = Query("name", pattern="^(name|newest|price_asc|price_desc)$"),
) -> dict[str, Any]:
    """Page through an entire game's upstream catalog (real ``total`` for paging),
    sorted server-side. Unsupported games return an empty page rather than an error."""
    _cache(response)
    return await catalog_browse_service.browse_catalog(game, page, page_size, sort=sort)


@router.get(
    "/trending",
    summary="Public trending (server-side sort / price ceiling)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_trending(
    response: Response,
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|all)$"),
    sort: str = Query("trending", pattern="^(trending|value)$"),
    max_price: float | None = Query(None, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Trending feed with the cut applied server-side.

    ``sort=value`` ("most valuable") draws from a dedicated high-priced source —
    not a re-sort of the newest-cards pool — so the rail is genuinely valuable.
    Every returned card is guaranteed to have a price *and* art, so the UI never
    renders a "—" priceless tile or a bare image.
    """
    _cache(response)
    if sort == "value":
        body = await trending_service.get_most_valuable(tcg=tcg, limit=100)
    else:
        body = await trending_service.get_trending(tcg=tcg, limit=100)

    # Only show cards we can actually price — a card with no market value reads
    # as broken in a shopping rail. `_market_amount` returns None when unpriced.
    cards = [c for c in (body.get("cards") or []) if _market_amount(c) is not None]

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
