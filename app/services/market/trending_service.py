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
import random
from collections.abc import Awaitable, Callable
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

# How many popular cards to pull per provider before the daily rotation
# picks the slice that surfaces today. A pool this size means a *different*
# set of popular cards leads the rail each day (instead of the same Sol Ring
# / Venusaur ex every single day) while staying within "genuinely popular".
_POOL_PER_PROVIDER = 50

# Hard cap per upstream provider when building the trending feed. The
# default `http_timeout_seconds` (15s) can occasionally hang under load,
# and the public `/cards/trending` endpoint must stay snappy because the
# home screen blocks on it. The Redis cache TTL (15 min) plus the client
# query persistence mean this cold-fetch cost is paid rarely, so we give
# providers 3s — enough for the (slightly heavier) ordered Pokémon query
# to land instead of timing out and collapsing the feed to two TCGs.
_PROVIDER_TIMEOUT_S = 5.0

#: Each provider's last *successful* trending pool is cached this long and
#: served as a fallback when a later fetch times out — so the mixed feed never
#: collapses to a single game just because one slow upstream (Pokemon / YGO
#: trending are erratic) blipped. Long because catalog identity is stable.
_POOL_TTL = 6 * 60 * 60

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


def _today_key() -> str:
    """UTC day stamp — rolls the trending rotation (and cache) over at midnight."""
    return datetime.now(UTC).strftime("%Y%m%d")


def _rotate_daily(items: list[dict[str, Any]], salt: int = 0) -> list[dict[str, Any]]:
    """Deterministically shuffle a pool by today's date.

    Same order all day (so the feed is stable while you browse), a fresh
    order tomorrow (so it doesn't show the identical cards every day). The
    ``salt`` keeps each provider's pool from shuffling in lockstep.
    """
    if len(items) <= 1:
        return items
    rng = random.Random(f"{_today_key()}:{salt}")
    shuffled = items[:]
    rng.shuffle(shuffled)
    return shuffled


def _has_art(card: dict[str, Any]) -> bool:
    """A card we can actually show — has artwork (no bare/placeholder rows)."""
    return bool(card.get("images") or card.get("image_url"))


def _distinct_by_name(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one card per name (first wins).

    A new set often has several printings of the same chase card (regular,
    alt-art, special illustration). They share a name, so showing all of them
    makes the rail look repetitive — one "Mega Greninja ex" is plenty.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in cards:
        key = (c.get("name") or "").strip().lower()
        if key and key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


async def _trending_pokemon(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    # `pokemon_tcg.search_cards` is already wrapped in the
    # ``"pokemontcg"`` circuit breaker at the integration layer
    # (see app/integrations/_http/pokemon_tcg.py).
    #
    # `orderBy=-set.releaseDate` surfaces chase cards from the NEWEST sets
    # first (Surging Sparks, Prismatic Evolutions, …) instead of the same
    # default-ordered handful. We pull a *pool* and drop art-less cards;
    # the caller rotates it daily so different chase cards lead each day.
    raw = await pokemon_tcg.search_cards(
        POKEMON_TRENDING_QUERY,
        page=1,
        page_size=min(MAX_LIMIT, pool),
        order_by="-set.releaseDate",
    )
    mapped = [_from_pokemon(c) for c in (raw.get("data") or [])]
    return _distinct_by_name([c for c in mapped if _has_art(c)])


async def _trending_magic(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    # Scryfall's `order` query param isn't surfaced by the shared
    # `scryfall.search_cards`, so we issue the request through the
    # resilient helper directly under the same ``"scryfall"`` breaker.
    # `order=edhrec` returns the most-played Commander cards (a genuine
    # popularity signal); a page is ~175 cards, so the daily rotation has a
    # deep pool of real staples to vary across — not just Sol Ring forever.
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
    return _distinct_by_name([c for c in mapped if _has_art(c)])[:pool]


async def _trending_yugioh(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    from app.config import get_settings

    s = get_settings()
    body = await request_json(
        integration="ygoprodeck",
        method="GET",
        url="https://db.ygoprodeck.com/api/v7/cardinfo.php",
        params={"num": pool, "offset": 0, "sort": "new"},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
        extra_ok_statuses=(400,),
    )
    if not body:
        return []
    mapped = [_from_yugioh(c) for c in (body.get("data") or [])]
    return _distinct_by_name([c for c in mapped if _has_art(c)])


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


async def _provider_pool(
    key: str,
    fetch: Callable[[], Awaitable[list[dict[str, Any]]]],
    fallback: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    """A provider's trending pool, with a long last-good + value fallback.

    Order: live trending → last-good cached pool → the game's ``valuable``
    source. Pokemon/YGO ``trending`` queries are erratic (circuit-open / slow)
    and often never populate a pool, so without the value fallback the mixed
    feed collapses to Magic. The value source is reliable, so it keeps every
    game represented. Never raises; caches whatever it resolves.
    """
    pool_key = f"loupe:cards:trendingpool:{key}"
    try:
        pool = await asyncio.wait_for(fetch(), _PROVIDER_TIMEOUT_S)
        if pool:
            await _cache_set(pool_key, {"items": pool}, _POOL_TTL)
            return pool
    except (TimeoutError, CircuitOpenError) as exc:
        logger.info("trending %s slow/open (%s); trying fallbacks", key, exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("trending %s failed (%s); trying fallbacks", key, exc)

    stale = await _cache_get(pool_key)
    items = stale.get("items") if isinstance(stale, dict) else None
    if isinstance(items, list) and items:
        return items

    if fallback is not None:
        try:
            vals = await asyncio.wait_for(fallback(), _VALUABLE_TIMEOUT_S)
            if vals:
                await _cache_set(pool_key, {"items": vals}, _POOL_TTL)
                return vals
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("trending %s value-fallback failed: %s", key, exc)
    return []


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

    # The day stamp in the key means a new day is a cache miss → fresh daily
    # rotation, while the 15-min TTL keeps it snappy within the day.
    cache_key = f"loupe:cards:trending:{tcg}:{limit}:{_today_key()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        cached_copy = dict(cached)
        cached_copy["source"] = "cached"
        return cached_copy

    # Per-provider salts for the daily shuffle so the three pools don't
    # rotate in lockstep.
    _SALT = {"pokemon": 1, "magic": 2, "yugioh": 3}

    cards: list[dict[str, Any]] = []
    try:
        if tcg == "pokemon":
            pool = await _provider_pool("pokemon", _trending_pokemon, _valuable_pokemon)
            cards = _rotate_daily(pool, _SALT["pokemon"])[:limit]
        elif tcg == "magic":
            pool = await _provider_pool("magic", _trending_magic, _valuable_magic)
            cards = _rotate_daily(pool, _SALT["magic"])[:limit]
        elif tcg == "yugioh":
            pool = await _provider_pool("yugioh", _trending_yugioh, _valuable_yugioh)
            cards = _rotate_daily(pool, _SALT["yugioh"])[:limit]
        else:
            # Each pool is live trending → last-good → value, so a slow/broken
            # Pokemon/YGO trending upstream no longer collapses the mix to Magic.
            pools = await asyncio.gather(
                _provider_pool("pokemon", _trending_pokemon, _valuable_pokemon),
                _provider_pool("magic", _trending_magic, _valuable_magic),
                _provider_pool("yugioh", _trending_yugioh, _valuable_yugioh),
            )
            lists = [
                _rotate_daily(pool, _SALT[key])
                for key, pool in zip(("pokemon", "magic", "yugioh"), pools, strict=True)
                if pool
            ]
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


# ── Most valuable ─────────────────────────────────────────────────────────
#
# "Trending" sorts the *newest* cards, which are often too new to have a
# price. "Most valuable" is the opposite cut: per provider we ask the catalog
# for its priciest cards (Scryfall `order=usd`, Pokémon by Cardmarket avg,
# YGO meta staples), keep only ones with a real price + art, and interleave
# the three lists so the rail is genuinely valuable *and* spans all games.


# "Most valuable" is a deliberate browse cut, cached daily and not on the
# blocking home-hero path, so we give its (sometimes cold) upstream queries a
# more generous budget than the snappy trending feed — otherwise a cold
# Scryfall/Pokémon fetch times out and the rail collapses to one game.
_VALUABLE_TIMEOUT_S = 7.0


def _price_of(card: dict[str, Any]) -> float | None:
    """Best numeric price for a card dict, or None when it has no price."""
    pricing = card.get("pricing_summary") or {}
    for key in ("market", "high", "mid", "low"):
        chosen = pricing.get(key)
        if isinstance(chosen, dict) and chosen.get("amount") is not None:
            try:
                return float(chosen["amount"])
            except (TypeError, ValueError):
                continue
    return None


def _priced_arted(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep distinct cards that have both a real price and artwork, priciest first."""
    out = [c for c in cards if _has_art(c) and _price_of(c) is not None]
    out.sort(key=lambda c: _price_of(c) or 0.0, reverse=True)
    return _distinct_by_name(out)


POKEMON_VALUABLE_QUERY = (
    '(rarity:"Special Illustration Rare" OR rarity:"Illustration Rare" '
    'OR rarity:"Hyper Rare" OR subtypes:VMAX OR subtypes:VSTAR OR subtypes:ex)'
)
SCRYFALL_VALUABLE_QUERY = "game:paper -is:digital"


async def _valuable_pokemon(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    raw = await pokemon_tcg.search_cards(
        POKEMON_VALUABLE_QUERY,
        page=1,
        page_size=min(MAX_LIMIT, pool * 2),
        order_by="-cardmarket.prices.averageSellPrice",
    )
    return _priced_arted([_from_pokemon(c) for c in (raw.get("data") or [])])[:pool]


async def _valuable_magic(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    from app.config import get_settings

    s = get_settings()
    body = await request_json(
        integration="scryfall",
        method="GET",
        url="https://api.scryfall.com/cards/search",
        params={"q": SCRYFALL_VALUABLE_QUERY, "order": "usd", "dir": "desc", "page": 1},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
    )
    if not body:
        return []
    return _priced_arted([_from_scryfall(c) for c in (body.get("data") or [])])[:pool]


async def _valuable_yugioh(pool: int = _POOL_PER_PROVIDER) -> list[dict[str, Any]]:
    from app.config import get_settings

    s = get_settings()
    body = await request_json(
        integration="ygoprodeck",
        method="GET",
        url="https://db.ygoprodeck.com/api/v7/cardinfo.php",
        params={"staple": "yes"},
        headers={"Accept": "application/json"},
        timeout_s=s.http_timeout_seconds,
        not_found_ok=True,
        extra_ok_statuses=(400,),
    )
    if not body:
        return []
    return _priced_arted([_from_yugioh(c) for c in (body.get("data") or [])])[:pool]


async def get_most_valuable(
    tcg: str = "all", limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Return the priciest cards (with prices + art), interleaved across games."""
    tcg = (tcg or "all").lower()
    limit = max(1, min(MAX_LIMIT, int(limit)))

    cache_key = f"loupe:cards:valuable:{tcg}:{limit}:{_today_key()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        out = dict(cached)
        out["source"] = "cached"
        return out

    cards: list[dict[str, Any]] = []
    try:
        if tcg == "pokemon":
            cards = (await asyncio.wait_for(_valuable_pokemon(), _VALUABLE_TIMEOUT_S))[
                :limit
            ]
        elif tcg == "magic":
            cards = (await asyncio.wait_for(_valuable_magic(), _VALUABLE_TIMEOUT_S))[
                :limit
            ]
        elif tcg == "yugioh":
            cards = (await asyncio.wait_for(_valuable_yugioh(), _VALUABLE_TIMEOUT_S))[
                :limit
            ]
        else:
            results = await asyncio.gather(
                asyncio.wait_for(_valuable_pokemon(), _VALUABLE_TIMEOUT_S),
                asyncio.wait_for(_valuable_magic(), _VALUABLE_TIMEOUT_S),
                asyncio.wait_for(_valuable_yugioh(), _VALUABLE_TIMEOUT_S),
                return_exceptions=True,
            )
            lists = [r for r in results if isinstance(r, list) and r]
            cards = _interleave(lists, limit)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("most-valuable unexpected error: %s", exc)
        cards = []

    envelope = {
        "cards": cards,
        "updated_at": _now_iso(),
        "source": "live" if cards else "empty",
    }
    if cards:
        await _cache_set(cache_key, envelope, TRENDING_TTL)
    return envelope


__all__ = ["get_most_valuable", "get_trending"]
