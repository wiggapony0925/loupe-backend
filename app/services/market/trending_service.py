"""Trending card aggregation service.

Mixes the three live catalogs (Pokémon TCG, Scryfall, YGOPRODeck) into a
single round-robin "trending" feed for the public ``/cards/trending``
endpoint.

Strategy per provider:

* **Pokémon TCG** — modern chase subtypes (ex / VMAX / VSTAR) ordered by
  `-set.releaseDate` so the *newest* sets' chase cards lead the rail
  (Surging Sparks, Prismatic Evolutions, …). This keeps the feed current
  and varied instead of returning the same default-ordered handful.
* **Scryfall** — `is:booster game:paper` with `order=edhrec` → the
  EDHREC popularity ordering surfaces genuinely-trending Commander
  staples.
* **YGOPRODeck** — `/cardinfo.php?num={N}&offset=0&sort=new` (newest
  releases). This is the closest thing the API exposes to "trending".

Art-less cards are dropped from every provider so the rail never shows a
bare placeholder row.

All three responses run in parallel via :func:`asyncio.gather`; any
single provider failure is logged and silently dropped. If everything
fails we fall back to a small hardcoded list of well-known card ids and
attempt a per-card ``get_card`` lookup; if even that fails we emit
minimal stubs so the endpoint NEVER returns a 5xx.

Responses are cached in Redis for :data:`~app.platform.cache_config.TRENDING_TTL`
seconds (15 min) under ``loupe:cards:trending:{tcg}:{limit}`` to keep
the rail snappy and stay well under provider rate budgets.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.integrations._http import pokemon_tcg
from app.integrations._http._resilient import request_json
from app.platform.cache_config import TRENDING_TTL
from app.platform.circuit_breaker import CircuitOpenError
from app.services.catalog import card_search_service
from app.services.catalog.card_search_service import (
    _cache_get,
    _cache_set,
    _from_pokemon,
    _from_scryfall,
    _from_yugioh,
    _interleave,
)

logger = logging.getLogger(__name__)

MAX_LIMIT = 100
DEFAULT_LIMIT = 24

# Hard cap per upstream provider when building the trending feed. The
# default `http_timeout_seconds` (15s) can occasionally hang under load,
# and the public `/cards/trending` endpoint must stay snappy because the
# home screen blocks on it. The Redis cache TTL (15 min) plus the client
# query persistence mean this cold-fetch cost is paid rarely, so we give
# providers 3s — enough for the (slightly heavier) ordered Pokémon query
# to land instead of timing out and collapsing the feed to two TCGs.
_PROVIDER_TIMEOUT_S = 3.0

# Per-provider trending queries. Chosen for: (a) high variety so the
# rail doesn't look like one card type, (b) low upstream cost, (c)
# alignment with what collectors actually chase right now.
#
# Pokémon TCG has no native "trending" sort, so we proxy it with the
# modern chase subtypes (ex / VMAX / VSTAR). These are the cards
# actually trading at premium right now and span dozens of Pokémon
# (Charizard, Pikachu, Mew, Gengar, Eevee, Lugia, etc.) instead of
# returning 60 Charizard variants in a row.
POKEMON_TRENDING_QUERY = "(subtypes:ex OR subtypes:VMAX OR subtypes:VSTAR)"
SCRYFALL_TRENDING_QUERY = "is:booster game:paper"

# Hardcoded fallback ids — must resolve via ``get_card`` if all live
# providers are down. Drawn from cards we've verified are stable across
# all three catalogs.
FALLBACK_IDS: tuple[str, ...] = (
    "pokemontcg:base1-4",  # Charizard, Base Set
    "pokemontcg:swsh4-25",  # Pikachu V
    "scryfall:e9d5aee0-5963-41db-a22b-cfea40a967a3",  # Black Lotus (alpha)
    "scryfall:9fa3df85-0a45-4e15-9e30-3ff48be19310",  # Liliana of the Veil
    "ygoprodeck:46986414",  # Dark Magician
    "ygoprodeck:89631139",  # Blue-Eyes White Dragon
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _has_art(card: dict[str, Any]) -> bool:
    """A card we can actually show — has artwork (no bare/placeholder rows)."""
    return bool(card.get("images") or card.get("image_url"))


async def _trending_pokemon(limit: int) -> list[dict[str, Any]]:
    # `pokemon_tcg.search_cards` is already wrapped in the
    # ``"pokemontcg"`` circuit breaker at the integration layer
    # (see app/integrations/_http/pokemon_tcg.py).
    #
    # `orderBy=-set.releaseDate` surfaces chase cards from the NEWEST sets
    # first (Surging Sparks, Prismatic Evolutions, …) instead of the same
    # default-ordered handful — so the rail feels current and varied. We
    # over-fetch and drop art-less cards so the feed is never "bare".
    raw = await pokemon_tcg.search_cards(
        POKEMON_TRENDING_QUERY,
        page=1,
        page_size=min(MAX_LIMIT, limit * 3),
        order_by="-set.releaseDate",
    )
    mapped = [_from_pokemon(c) for c in (raw.get("data") or [])]
    return [c for c in mapped if _has_art(c)][:limit]


async def _trending_magic(limit: int) -> list[dict[str, Any]]:
    # Scryfall's `order` query param isn't surfaced by the shared
    # `scryfall.search_cards`, so we issue the request through the
    # resilient helper directly under the same ``"scryfall"`` breaker.
    from app.config import get_settings

    s = get_settings()
    body = await request_json(
        integration="scryfall",
        method="GET",
        url="https://api.scryfall.com/cards/search",
        params={
            "q": SCRYFALL_TRENDING_QUERY,
            "order": "edhrec",
            "page": 1,
        },
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
    )
    if body is None:
        return []
    mapped = [_from_scryfall(c) for c in (body.get("data") or [])]
    return [c for c in mapped if _has_art(c)][:limit]


async def _trending_yugioh(limit: int) -> list[dict[str, Any]]:
    from app.config import get_settings

    s = get_settings()
    body = await request_json(
        integration="ygoprodeck",
        method="GET",
        url="https://db.ygoprodeck.com/api/v7/cardinfo.php",
        params={"num": limit, "offset": 0, "sort": "new"},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
        extra_ok_statuses=(400,),
    )
    if not body:
        return []
    return [_from_yugioh(c) for c in (body.get("data") or [])][:limit]


async def _fallback_cards(limit: int) -> list[dict[str, Any]]:
    """Last-resort: try resolving hardcoded ids individually."""
    ids = FALLBACK_IDS[:limit] or FALLBACK_IDS
    results = await asyncio.gather(
        *(card_search_service.get_card(cid) for cid in ids),
        return_exceptions=True,
    )
    out: list[dict[str, Any]] = []
    for cid, res in zip(ids, results, strict=False):
        if isinstance(res, BaseException) or res is None:
            # Minimal stub so the UI can render *something*.
            out.append(
                {
                    "id": cid,
                    "name": "Unavailable",
                    "tcg": cid.split(":", 1)[0],
                    "images": None,
                    "image_url": None,
                    "set_name": None,
                    "year": None,
                    "number": None,
                    "rarity": None,
                    "pricing_summary": None,
                    "source": "fallback",
                    "attributes": None,
                }
            )
        else:
            out.append(res)
    return out


async def get_trending(tcg: str = "all", limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return a trending feed envelope.

    Parameters
    ----------
    tcg:
        ``"pokemon"`` | ``"magic"`` | ``"yugioh"`` | ``"all"`` (default).
    limit:
        Max cards in the final list, 1–48. Defaults to 24.

    Returns
    -------
    dict
        ``{"cards": [...], "updated_at": iso8601, "source": "live"|"cached"|"fallback"}``.
        Never raises and never returns 5xx upstream.
    """
    tcg = (tcg or "all").lower()
    limit = max(1, min(MAX_LIMIT, int(limit)))

    cache_key = f"loupe:cards:trending:{tcg}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        cached_copy = dict(cached)
        cached_copy["source"] = "cached"
        return cached_copy

    cards: list[dict[str, Any]] = []
    try:
        if tcg == "pokemon":
            cards = (
                await asyncio.wait_for(_trending_pokemon(limit), _PROVIDER_TIMEOUT_S)
            )[:limit]
        elif tcg == "magic":
            cards = (
                await asyncio.wait_for(_trending_magic(limit), _PROVIDER_TIMEOUT_S)
            )[:limit]
        elif tcg == "yugioh":
            cards = (
                await asyncio.wait_for(_trending_yugioh(limit), _PROVIDER_TIMEOUT_S)
            )[:limit]
        else:
            per = max(4, (limit // 3) + 2)
            results = await asyncio.gather(
                asyncio.wait_for(_trending_pokemon(per), _PROVIDER_TIMEOUT_S),
                asyncio.wait_for(_trending_magic(per), _PROVIDER_TIMEOUT_S),
                asyncio.wait_for(_trending_yugioh(per), _PROVIDER_TIMEOUT_S),
                return_exceptions=True,
            )
            lists: list[list[dict[str, Any]]] = []
            for label, res in zip(
                ("pokemontcg", "scryfall", "ygoprodeck"),
                results,
                strict=False,
            ):
                if isinstance(res, CircuitOpenError):
                    logger.debug(
                        "trending upstream %s skipped (circuit open): %s",
                        label,
                        res,
                    )
                    continue
                if isinstance(res, BaseException):
                    logger.warning("trending upstream %s failed: %s", label, res)
                    continue
                if isinstance(res, list) and res:
                    lists.append(res)
            cards = _interleave(lists, limit)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("trending unexpected error: %s", exc)
        cards = []

    source = "live"
    if not cards:
        try:
            cards = await _fallback_cards(limit)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("trending fallback failed: %s", exc)
            cards = []
        source = "fallback"

    envelope = {
        "cards": cards,
        "updated_at": _now_iso(),
        "source": source,
    }

    # Cache live + fallback responses alike so we don't hammer providers
    # while they're down. TTL is short enough that a recovery propagates
    # within 15 min.
    await _cache_set(cache_key, envelope, TRENDING_TTL)
    return envelope


__all__ = ["get_trending"]
