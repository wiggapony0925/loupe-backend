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
from app.integrations._http import digimoncard, pokemon_tcg
from app.integrations._http._resilient import request_json
from app.services.catalog.card_search_service import (
    _cache_get,
    _cache_set,
    _from_digimon,
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

#: One retry on a degraded fetch. These full-catalog ``orderBy`` queries are
#: slow and flaky upstream (notably pokemontcg.io on ``-set.releaseDate`` /
#: ``cardmarket.prices.*``): they intermittently time out *or* answer a 200 with
#: a real ``totalCount`` but an empty ``data`` array. A single retry recovers the
#: common transient case without hammering a struggling upstream.
_MAX_FETCH_ATTEMPTS = 2

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


def _is_degraded(result: dict[str, Any], page: int, page_size: int) -> bool:
    """True when a page came back empty but the total says it shouldn't have.

    A page is *legitimately* empty when its offset is past the end of the
    catalog (``page`` beyond the last page) or the total is zero — that's a real
    "no more rows", not a failure. But an empty page whose offset falls *within*
    the reported total is a degraded upstream response (a 200 with ``totalCount``
    populated but ``data: []``) — the ``total=20359, cards=[]`` "newest is broken"
    bug. Those are worth a retry."""
    if result.get("cards"):
        return False
    total = int(result.get("total") or 0)
    return total > 0 and (page - 1) * page_size < total


async def browse_catalog(
    game: str, page: int, page_size: int, sort: str = "name", set_id: str | None = None
) -> dict[str, Any]:
    """One page of a game's catalog — cached, sorted server-side, never raises.

    Serves from Redis when warm; on a cold miss fetches upstream. If the upstream
    is slow / down / circuit-open, returns an empty page (never a 500) so the
    client degrades cleanly instead of hanging or erroring.

    The full-catalog ``orderBy`` queries (especially ``newest`` →
    ``-set.releaseDate`` and the price sorts) are slow and flaky upstream: they
    intermittently time out or answer a 200 with a real ``totalCount`` but an
    empty ``data`` array. We retry such a degraded page once (see
    :data:`_MAX_FETCH_ATTEMPTS`) so a transient blip doesn't surface as an empty
    page that contradicts the row count.

    When ``set_id`` is given (e.g. ``pokemontcg:base1``) the page is scoped to
    that single set — powering the "browse a set" surface. Supported for Pokémon
    and Magic; other games return an empty page for a set filter.
    """
    game = (game or "").lower()
    sort = sort if sort in _POKEMON_ORDER else "name"

    set_key = (set_id or "").strip() or "_"
    cache_key = f"{_CACHE_PREFIX}:{game}:{set_key}:{sort}:{page}:{page_size}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    result = _empty(game, page, page_size)
    for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
        try:
            result = await _fetch_catalog(game, page, page_size, sort, set_id)
        except Exception as exc:
            logger.warning(
                "browse_catalog upstream failed game=%s set=%s page=%s sort=%s "
                "attempt=%s/%s: %s",
                game,
                set_key,
                page,
                sort,
                attempt,
                _MAX_FETCH_ATTEMPTS,
                exc,
            )
            result = _empty(game, page, page_size)
            continue

        if not _is_degraded(result, page, page_size):
            break  # got rows, or a legitimately empty (out-of-range) page

        logger.warning(
            "browse_catalog empty-but-counted game=%s set=%s page=%s sort=%s "
            "total=%s attempt=%s/%s",
            game,
            set_key,
            page,
            sort,
            result.get("total"),
            attempt,
            _MAX_FETCH_ATTEMPTS,
        )

    if result.get("cards"):
        await _cache_set(cache_key, result, _BROWSE_TTL)
    return result


async def _fetch_catalog(
    game: str, page: int, page_size: int, sort: str, set_id: str | None = None
) -> dict[str, Any]:
    """Fetch one catalog page from the right upstream. May raise on upstream failure."""
    s = get_settings()

    # Provider-local set code (drop the "<source>:" prefix, e.g. pokemontcg:base1).
    prov_set = set_id.split(":")[-1].strip() if set_id else ""

    if game in ("pokemon", "all"):
        # Empty query → full catalog; `set.id:<code>` scopes to one set.
        q = f"set.id:{prov_set}" if prov_set else ""
        raw = await pokemon_tcg.search_cards(
            q, page=page, page_size=page_size, order_by=_POKEMON_ORDER[sort]
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
                "q": f"set:{prov_set}" if prov_set else _MAGIC_ALL,
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
        # No clean per-set browse on this provider yet — a set filter returns an
        # empty page rather than the whole game (which would be wrong).
        if prov_set:
            return _empty(game, page, page_size)
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

    if game == "digimon":
        # digimoncard.io has no native paging — fetch the full (immutable)
        # catalog once, cache it normalized, then slice in-process.
        cards = await _digimon_catalog()
        if prov_set:
            cards = [
                c
                for c in cards
                if (c.get("set_code") or "").lower() == prov_set.lower()
            ]
        total = len(cards)
        start = (page - 1) * page_size
        return {
            "cards": cards[start : start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "source": "digimoncard",
        }

    # Unsupported game (lorcana / onepiece) — graceful empty.
    return _empty(game, page, page_size)


_DIGIMON_CATALOG_KEY = "loupe:public:browse:digimon:_full"
_DIGIMON_CATALOG_TTL = 86_400  # 24h — the Digimon catalog is effectively static.


async def _digimon_catalog() -> list[dict[str, Any]]:
    """Full Digimon catalog, normalized + name-sorted, cached for a day."""
    cached = await _cache_get(_DIGIMON_CATALOG_KEY)
    if isinstance(cached, dict) and isinstance(cached.get("cards"), list):
        return cached["cards"]
    raw = await digimoncard.list_all()
    cards = [_from_digimon(c) for c in raw]
    cards.sort(key=lambda c: (c.get("name") or "").lower())
    if cards:
        await _cache_set(_DIGIMON_CATALOG_KEY, {"cards": cards}, _DIGIMON_CATALOG_TTL)
    return cards


__all__ = ["browse_catalog"]
