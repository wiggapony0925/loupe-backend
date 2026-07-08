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

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from app.platform.rate_limit import catalog_read_limit, search_live_limit
from app.services.catalog import (
    card_search_service,
    carousel_service,
    catalog_browse_service,
    games,
)
from app.services.market import trending_service

router = APIRouter(prefix="/public", tags=["public"])


@router.get(
    "/games",
    summary="Storefront category tags (games + sections; backend-driven)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_games(response: Response) -> dict[str, Any]:
    """The canonical marketplace/search category tags both web + mobile render,
    each with a ``status`` (``live`` | ``soon``) derived from real catalog
    capability — so One Piece flips live everywhere from here, no client
    release. App-only actions (Scan/Grade/Scanner) stay client-side."""
    _cache(response)
    return games.catalog_tags()


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
        "all", pattern="^(pokemon|magic|yugioh|digimon|onepiece|lorcana|sports|all)$"
    ),
    rarity: str | None = Query(None, max_length=80),
    set_name: str | None = Query(None, alias="set", max_length=120),
    sort: str = Query("best", pattern="^(best|price_asc|price_desc|name)$"),
    langs: str = Query(
        "en",
        max_length=80,
        description="Comma-separated ISO languages to include (default English).",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=60),
) -> dict[str, Any]:
    """Search (or, with no query, browse trending) with all derivation done here.

    Returns the paginated slice plus ``total``, ``facets`` and
    ``available_languages`` so the client renders + drives its language picker
    directly — no client-side filtering/sorting/pagination.
    """
    _cache(response)
    lang_list = [x for x in (s.strip().lower() for s in langs.split(",")) if x] or [
        "en"
    ]
    langs_avail = await card_search_service.available_languages(
        tcg if tcg != "all" else None
    )
    if q.strip():
        # Default experience (relevance sort, no facet filters): TRUE upstream
        # pagination, so every printing of a popular name is reachable —
        # Pikachu has 177, Charizard 400+; the old pooled top-60 made the
        # catalog look like it was missing most of them.
        if rarity is None and set_name is None and sort == "best" and page_size > 12:
            paged = await card_search_service.search_cards_paged(
                q=q, tcg=tcg, page=page, page_size=page_size, langs=lang_list
            )
            items = list(paged.get("results") or [])
            return {
                "results": items,
                "total": paged.get("total", len(items)),
                "page": page,
                "page_size": page_size,
                # Facets come from the visible page — options accumulate as
                # the user pages; filtering re-enters the pooled path below.
                "facets": {
                    "rarities": sorted(
                        {c.get("rarity") for c in items if c.get("rarity")}
                    ),
                    "sets": sorted(
                        {c.get("set_name") for c in items if c.get("set_name")}
                    ),
                },
                "available_languages": langs_avail,
                "source": paged.get("source"),
            }
        # Typeahead (small page_size) and filtered/price-sorted views work on
        # a ranked pool — filters and price sorts need the whole set in hand.
        fetch_limit = 20 if page_size <= 12 else 60
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
        "available_languages": langs_avail,
        "source": source,
    }


@router.get(
    "/sparklines",
    summary="Batch mini price series for a set of cards (list-row sparklines)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_sparklines(
    response: Response,
    ids: str = Query(..., max_length=2000),
    range_: str = Query("7d", alias="range", pattern="^(7d|30d|90d)$"),
) -> dict[str, Any]:
    """A tiny trend series per id so list rows (search dropdown, rails) can
    show a Robinhood-style sparkline without one request per card.

    Reuses the cached, synthesized price history, so a batch of visible ids
    resolves from cache after the first hit. Capped to keep the fan-out small.
    """
    _cache(response)
    id_list = [i.strip() for i in ids.split(",") if i.strip()][:24]
    histories = await asyncio.gather(
        *(card_search_service.get_price_history(cid, range_) for cid in id_list)
    )
    out: list[dict[str, Any]] = []
    for cid, body in zip(id_list, histories, strict=True):
        if not body:
            continue
        points = [
            p["price"]
            for p in (body.get("points") or [])
            if isinstance(p, dict) and p.get("price") is not None
        ]
        summary = body.get("summary") or {}
        out.append(
            {
                "card_id": cid,
                "points": points,
                "change_pct": summary.get("change_pct"),
            }
        )
    return {"sparklines": out}


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
    set_id: str | None = Query(None, alias="set", max_length=120),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=50),
    sort: str = Query("name", pattern="^(name|newest|price_asc|price_desc)$"),
) -> dict[str, Any]:
    """Page through an entire game's upstream catalog (real ``total`` for paging),
    sorted server-side. With ``set`` (e.g. ``pokemontcg:base1``) the page is scoped
    to a single set. Unsupported games return an empty page rather than an error."""
    _cache(response)
    return await catalog_browse_service.browse_catalog(
        game, page, page_size, sort=sort, set_id=set_id
    )


@router.get(
    "/carousels",
    summary="AI-designed marketplace carousels (recipes) for a game",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_carousels(
    response: Response,
    game: str = Query("pokemon", pattern="^(pokemon|magic|yugioh|onepiece|digimon)$"),
) -> dict[str, Any]:
    """A game's discovery shelves as serializable recipes (theme + filter), AI-
    designed and cached daily. The web compiles each into a real rail and mixes
    them into the rotating storefront; ``source`` says whether the model
    produced them (``ai``) or it fell back to the web's curated pool
    (``curated``)."""
    _cache(response)
    return (await carousel_service.get_carousels(game)).model_dump()


@router.get(
    "/trending",
    summary="Public trending (server-side sort / price ceiling)",
    dependencies=[Depends(catalog_read_limit)],
)
async def public_trending(
    response: Response,
    tcg: str = Query("all", pattern="^(pokemon|magic|yugioh|digimon|onepiece|all)$"),
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
    # Single source of truth shared with the mobile `/v1/cards/trending`
    # endpoint: priced-only, art-only, sorted, trending≠valuable deduped, and
    # the catalog-only-game empty-rail guard all live in `get_shelf`, so the two
    # clients can never diverge.
    return await trending_service.get_shelf(
        tcg=tcg, sort=sort, max_price=max_price, limit=limit
    )


__all__ = ["router"]
