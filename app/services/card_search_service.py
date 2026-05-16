"""Live card-catalog search service.

Wraps the upstream HTTP clients (Scryfall, Pokémon TCG, YGOPRODeck) and
normalises their responses into a single ``UnifiedCard`` shape consumed by
the public ``/cards/search`` and ``/cards/{id}`` endpoints.

All calls degrade gracefully: upstream errors become an empty result list
with an ``error`` field so the mobile client never has to handle 5xx.
Results are cached in Redis (5 min for search, 24 h for individual cards
and set listings) with an in-process fallback when Redis isn't reachable.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import httpx

from app.cache_config import CARD_DETAIL_TTL, CARD_SEARCH_TTL, SET_LIST_TTL
from app.clients import pokemon_tcg, scryfall, ygoprodeck
from app.clients.redis_client import get_redis
from app.utils.logger import get_logger

logger = get_logger("services.card_search")

Tcg = Literal["pokemon", "magic", "yugioh", "all"]
Source = Literal["pokemontcg", "scryfall", "ygoprodeck"]

MAX_LIMIT = 50


# --------------------------------------------------------------------- shape


def _empty(tcg: str, error: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"results": [], "total": 0, "source": _source_for(tcg)}
    if error:
        body["error"] = error
    return body


def _source_for(tcg: str) -> str:
    return {
        "pokemon": "pokemontcg",
        "magic": "scryfall",
        "yugioh": "ygoprodeck",
        "all": "scryfall",
    }.get(tcg, tcg)


def _cap(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return 20
    return min(limit, MAX_LIMIT)


# ------------------------------------------------------------------ adapters


def _from_pokemon(card: dict[str, Any]) -> dict[str, Any]:
    images = card.get("images") or {}
    set_obj = card.get("set") or {}
    return {
        "id": f"pokemontcg:{card.get('id')}",
        "name": card.get("name") or "",
        "tcg": "pokemon",
        "set_name": set_obj.get("name"),
        "set_code": set_obj.get("id") or set_obj.get("ptcgoCode"),
        "number": card.get("number"),
        "rarity": card.get("rarity"),
        "image_url": images.get("small") or images.get("large"),
        "year": _year(set_obj.get("releaseDate")),
        "source": "pokemontcg",
    }


def _from_scryfall(card: dict[str, Any]) -> dict[str, Any]:
    image_uris = card.get("image_uris") or {}
    if not image_uris and card.get("card_faces"):
        faces = card["card_faces"]
        if faces and isinstance(faces[0], dict):
            image_uris = faces[0].get("image_uris") or {}
    return {
        "id": f"scryfall:{card.get('id')}",
        "name": card.get("name") or "",
        "tcg": "magic",
        "set_name": card.get("set_name"),
        "set_code": card.get("set"),
        "number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "image_url": image_uris.get("normal") or image_uris.get("small"),
        "year": _year(card.get("released_at")),
        "source": "scryfall",
    }


def _from_yugioh(card: dict[str, Any]) -> dict[str, Any]:
    images = (card.get("card_images") or [{}])[0]
    sets = card.get("card_sets") or [{}]
    first_set = sets[0] if sets else {}
    return {
        "id": f"ygoprodeck:{card.get('id')}",
        "name": card.get("name") or "",
        "tcg": "yugioh",
        "set_name": first_set.get("set_name"),
        "set_code": first_set.get("set_code"),
        "number": first_set.get("set_code"),
        "rarity": first_set.get("set_rarity"),
        "image_url": images.get("image_url_small") or images.get("image_url"),
        "year": None,
        "source": "ygoprodeck",
    }


_YEAR_RE = re.compile(r"(\d{4})")


def _year(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    m = _YEAR_RE.search(value)
    return int(m.group(1)) if m else None


# -------------------------------------------------------------------- search


async def _cache_get(key: str) -> dict[str, Any] | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # pragma: no cover - cache best-effort
        logger.debug("cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: dict[str, Any], ttl: int) -> None:
    try:
        r = await get_redis()
        await r.setex(key, ttl, json.dumps(value))
    except Exception as exc:  # pragma: no cover
        logger.debug("cache_set failed: %s", exc)


async def search_cards(q: str, tcg: str, limit: int) -> dict[str, Any]:
    """Search live upstream catalog and return a unified envelope."""
    q = (q or "").strip()
    tcg = (tcg or "all").lower()
    limit = _cap(limit)
    if not q:
        return _empty(tcg)

    cache_key = f"loupe:cards:search:{tcg}:{q.lower()}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if tcg == "pokemon":
            raw = await pokemon_tcg.search_cards(
                f'name:"{q}*"', page=1, page_size=limit
            )
            items = [_from_pokemon(c) for c in (raw.get("data") or [])][:limit]
            total = int(raw.get("totalCount") or len(items))
            body = {"results": items, "total": total, "source": "pokemontcg"}
        elif tcg == "yugioh":
            raw = await ygoprodeck.search_cards(q)
            data = raw.get("data") or []
            items = [_from_yugioh(c) for c in data[:limit]]
            body = {"results": items, "total": len(data), "source": "ygoprodeck"}
        else:  # magic or all → Scryfall
            raw = await scryfall.search_cards(q, page=1)
            data = raw.get("data") or []
            items = [_from_scryfall(c) for c in data[:limit]]
            body = {
                "results": items,
                "total": int(raw.get("total_cards") or len(items)),
                "source": "scryfall",
            }
    except httpx.HTTPError as exc:
        logger.warning("upstream search failed (%s): %s", tcg, exc)
        return _empty(tcg, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected error in search_cards: %s", exc)
        return _empty(tcg, error="upstream error")

    await _cache_set(cache_key, body, CARD_SEARCH_TTL)
    return body


# --------------------------------------------------------------------- single


async def get_card(card_id: str) -> dict[str, Any] | None:
    """Look up a single card by composite ``<source>:<upstream_id>`` ID."""
    if ":" not in card_id:
        return None
    source, _, upstream_id = card_id.partition(":")
    source = source.lower()
    if not upstream_id:
        return None

    cache_key = f"loupe:cards:item:{source}:{upstream_id}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if source == "pokemontcg":
            raw = await pokemon_tcg.get_card(upstream_id)
            result = _from_pokemon(raw) if raw else None
        elif source == "scryfall":
            raw = await scryfall.get_card(upstream_id)
            result = _from_scryfall(raw) if raw else None
        elif source == "ygoprodeck":
            try:
                raw = await ygoprodeck.get_card(int(upstream_id))
            except ValueError:
                return None
            result = _from_yugioh(raw) if raw else None
        else:
            return None
    except httpx.HTTPError as exc:
        logger.warning("upstream get_card failed (%s): %s", source, exc)
        return None

    if result is not None:
        await _cache_set(cache_key, result, CARD_DETAIL_TTL)
    return result


# ------------------------------------------------------------------ set list


def _scryfall_set(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"scryfall:{s.get('id')}",
        "code": s.get("code"),
        "name": s.get("name"),
        "tcg": "magic",
        "release_date": s.get("released_at"),
        "total_cards": s.get("card_count"),
        "image_url": s.get("icon_svg_uri"),
        "source": "scryfall",
    }


def _pokemon_set(s: dict[str, Any]) -> dict[str, Any]:
    images = s.get("images") or {}
    return {
        "id": f"pokemontcg:{s.get('id')}",
        "code": s.get("id") or s.get("ptcgoCode"),
        "name": s.get("name"),
        "tcg": "pokemon",
        "release_date": s.get("releaseDate"),
        "total_cards": s.get("total"),
        "image_url": images.get("logo") or images.get("symbol"),
        "source": "pokemontcg",
    }


def _ygo_set(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"ygoprodeck:{s.get('set_code')}",
        "code": s.get("set_code"),
        "name": s.get("set_name"),
        "tcg": "yugioh",
        "release_date": s.get("tcg_date"),
        "total_cards": s.get("num_of_cards"),
        "image_url": None,
        "source": "ygoprodeck",
    }


async def list_sets(tcg: str) -> dict[str, Any]:
    """Live proxy of the upstream set lists."""
    tcg = (tcg or "all").lower()
    cache_key = f"loupe:sets:{tcg}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if tcg == "pokemon":
            sets = await pokemon_tcg.list_sets()
            items = [_pokemon_set(s) for s in sets]
            body = {"results": items, "total": len(items), "source": "pokemontcg"}
        elif tcg == "yugioh":
            sets = await ygoprodeck.list_sets()
            items = [_ygo_set(s) for s in sets]
            body = {"results": items, "total": len(items), "source": "ygoprodeck"}
        else:
            sets = await scryfall.list_sets()
            items = [_scryfall_set(s) for s in sets]
            body = {"results": items, "total": len(items), "source": "scryfall"}
    except httpx.HTTPError as exc:
        logger.warning("upstream list_sets failed (%s): %s", tcg, exc)
        return {
            "results": [],
            "total": 0,
            "source": _source_for(tcg),
            "error": str(exc),
        }

    await _cache_set(cache_key, body, SET_LIST_TTL)
    return body


__all__ = ["get_card", "list_sets", "search_cards"]
