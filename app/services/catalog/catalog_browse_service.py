"""Catalog browse — page through an entire game's upstream catalog.

Powers ``GET /v1/public/browse`` so the web storefront can browse *thousands*
of cards per game (not just the ~70 trending). Reuses the card converters from
:mod:`card_search_service` and the resilient HTTP layer. Read-only, no DB.

Pagination is normalized to a single ``page``/``page_size`` contract across
three upstreams with different native paging:

* **Pokémon TCG** — native ``page``/``pageSize`` + ``totalCount``.
* **Scryfall** — fixed 175-card pages; we fetch the page covering the requested
  global offset and slice it to ``page_size``.
* **YGOPRODeck** — native ``num``/``offset`` + ``meta.total_rows``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.integrations._http import pokemon_tcg
from app.integrations._http._resilient import request_json
from app.services.catalog.card_search_service import (
    _cache_get,
    _cache_set,
    _from_pokemon,
    _from_scryfall,
    _from_yugioh,
)

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "loupe:public:browse"
_BROWSE_TTL = (
    900  # 15 min — catalog pages change rarely; serve hot + survive upstream blips.
)

_SCRYFALL_PAGE = 175  # Scryfall fixes its page size.
_MAGIC_ALL = "game:paper"  # every paper Magic printing

# Unified sort → per-upstream native ordering.
_POKEMON_ORDER = {
    "name": "name",
    "newest": "-set.releaseDate",
    "price_asc": "cardmarket.prices.averageSellPrice",
    "price_desc": "-cardmarket.prices.averageSellPrice",
}
_SCRYFALL_ORDER = {
    "name": ("name", "asc"),
    "newest": ("released", "desc"),
    "price_asc": ("usd", "asc"),
    "price_desc": ("usd", "desc"),
}
_YGO_SORT = {"name": "name", "newest": "new", "price_asc": "name", "price_desc": "name"}


def _empty(game: str, page: int, page_size: int) -> dict[str, Any]:
    return {
        "cards": [],
        "total": 0,
        "page": page,
        "page_size": page_size,
        "source": game,
    }


async def browse_catalog(
    game: str, page: int, page_size: int, sort: str = "name"
) -> dict[str, Any]:
    """One page of a game's catalog — cached, sorted server-side, never raises.

    Serves from Redis when warm; on a cold miss fetches upstream. If the upstream
    is slow / down / circuit-open, returns an empty page (never a 500) so the
    client degrades cleanly instead of hanging or erroring.
    """
    game = (game or "").lower()
    sort = sort if sort in _POKEMON_ORDER else "name"

    cache_key = f"{_CACHE_PREFIX}:{game}:{sort}:{page}:{page_size}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        result = await _fetch_catalog(game, page, page_size, sort)
    except Exception as exc:
        logger.warning(
            "browse_catalog upstream failed game=%s page=%s sort=%s: %s",
            game,
            page,
            sort,
            exc,
        )
        return _empty(game, page, page_size)

    if result.get("cards"):
        await _cache_set(cache_key, result, _BROWSE_TTL)
    return result


async def _fetch_catalog(
    game: str, page: int, page_size: int, sort: str
) -> dict[str, Any]:
    """Fetch one catalog page from the right upstream. May raise on upstream failure."""
    s = get_settings()

    if game in ("pokemon", "all"):
        # Empty query → the Pokémon TCG API returns the full catalog.
        raw = await pokemon_tcg.search_cards(
            "", page=page, page_size=page_size, order_by=_POKEMON_ORDER[sort]
        )
        cards = [_from_pokemon(c) for c in (raw.get("data") or [])]
        total = int(raw.get("totalCount") or len(cards))
        return {
            "cards": cards,
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "pokemontcg",
        }

    if game == "magic":
        offset = (page - 1) * page_size
        sf_page = offset // _SCRYFALL_PAGE + 1
        within = offset % _SCRYFALL_PAGE
        order, direction = _SCRYFALL_ORDER[sort]
        body = await request_json(
            integration="scryfall",
            method="GET",
            url="https://api.scryfall.com/cards/search",
            params={
                "q": _MAGIC_ALL,
                "order": order,
                "dir": direction,
                "unique": "prints",
                "page": sf_page,
            },
            headers={"Accept": "application/json"},
            timeout_s=s.http_timeout_seconds,
            not_found_ok=True,
        )
        if not body:
            return _empty(game, page, page_size)
        data = body.get("data") or []
        cards = [_from_scryfall(c) for c in data[within : within + page_size]]
        total = int(body.get("total_cards") or len(cards))
        return {
            "cards": cards,
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "scryfall",
        }

    if game == "yugioh":
        offset = (page - 1) * page_size
        body = await request_json(
            integration="ygoprodeck",
            method="GET",
            url="https://db.ygoprodeck.com/api/v7/cardinfo.php",
            params={"num": page_size, "offset": offset, "sort": _YGO_SORT[sort]},
            headers={"Accept": "application/json"},
            timeout_s=s.http_timeout_seconds,
            not_found_ok=True,
            extra_ok_statuses=(400,),
        )
        if not body:
            return _empty(game, page, page_size)
        cards = [_from_yugioh(c) for c in (body.get("data") or [])]
        meta = body.get("meta") or {}
        total = int(meta.get("total_rows") or 0) or (offset + len(cards))
        return {
            "cards": cards,
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "ygoprodeck",
        }

    # Unsupported game (lorcana / onepiece / digimon) — graceful empty.
    return _empty(game, page, page_size)


__all__ = ["browse_catalog"]
