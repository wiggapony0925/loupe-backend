"""Live card-catalog search service.

Wraps the upstream HTTP clients (Scryfall, Pokémon TCG, YGOPRODeck) and
normalises their responses into a single rich ``UnifiedCard`` shape
consumed by ``/cards/search`` and ``/cards/{id}``.

All calls degrade gracefully: upstream errors become an empty result list
with an ``error`` field so the mobile client never has to handle 5xx.
Results are cached in Redis (5 min for search, 24 h for individual cards
and set listings) with an in-process fallback when Redis isn't reachable.

Multi-provider mode (``tcg=all``) fans out to all three upstreams in
parallel and interleaves the results round-robin so the list isn't
dominated by a single provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from datetime import timedelta
from typing import Any

import httpx

from app.integrations._http import pokemon_tcg, scryfall, ygoprodeck
from app.platform.cache_config import (
    CARD_DETAIL_TTL,
    CARD_SEARCH_TTL,
    PRICE_HISTORY_TTL,
    SET_LIST_TTL,
)
from app.platform.circuit_breaker import CircuitOpenError
from app.platform.redis_client import get_redis
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("services.card_search")

MAX_LIMIT = 50
#: Hard ceiling per provider in the ``tcg=all`` fan-out. Healthy upstream
#: responses come back in 300–800 ms; this cap exists purely to keep one
#: misbehaving provider from holding the entire keystroke hostage.
#: Partial failures are NOT cached for long (see ``PARTIAL_RESULT_TTL``)
#: so a brief blip self-heals on the next keystroke.
PER_PROVIDER_TIMEOUT = 3.0

#: Soft deadline: once this elapses, if at least two providers have
#: already returned we ship the response and cancel the laggard.
#: This means a slow Pokemon TCG no longer blocks Magic+Yu-Gi-Oh
#: from rendering — the user sees something within ~1.5s instead of 3s+.
FAST_SETTLE_DEADLINE = 1.5

#: Cache TTL when one or more providers in a ``tcg=all`` fan-out failed.
#: Short enough that a recovered provider shows up on the next keystroke,
#: long enough that rapid retyping doesn't hammer the upstream.
PARTIAL_RESULT_TTL = 20

#: Providers we *don't* have an upstream for yet — returned gracefully.
UNSUPPORTED_TCGS = {"onepiece", "lorcana", "sports"}


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
        "all": "mixed",
        "onepiece": "onepiece",
        "lorcana": "lorcana",
        "sports": "sports",
    }.get(tcg, tcg)


def _cap(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return 20
    return min(limit, MAX_LIMIT)


_YEAR_RE = re.compile(r"(\d{4})")


def _year(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    m = _YEAR_RE.search(value)
    return int(m.group(1)) if m else None


def _img(url: str | None, alt: str | None = None) -> dict[str, Any] | None:
    if not url:
        return None
    return {"url": url, "width": None, "height": None, "alt": alt}


def _now_iso() -> str:
    return utcnow().isoformat()


def _meta(source: str) -> dict[str, Any]:
    return {"source": source, "last_synced_at": _now_iso(), "confidence": 1.0}


def _money(amount: Any) -> dict[str, Any] | None:
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return {"amount": round(v, 2), "currency": "USD"}


# ------------------------------------------------------------------ adapters


def _from_pokemon(card: dict[str, Any]) -> dict[str, Any]:
    """Map a Pokémon TCG API card to the unified rich shape."""
    images = card.get("images") or {}
    set_obj = card.get("set") or {}
    set_images = set_obj.get("images") or {}
    rarity = card.get("rarity") or ""
    subtypes = card.get("subtypes") or []
    name = card.get("name") or ""

    # Tags
    tags: list[str] = []
    if any("holo" in (s or "").lower() for s in subtypes) or "holo" in rarity.lower():
        tags.append("holo")
    if "1st" in (card.get("number") or "") or "First Edition" in rarity:
        tags.append("first_edition")
    if "promo" in rarity.lower():
        tags.append("promo")

    year = _year(set_obj.get("releaseDate"))
    if year and year < 2000:
        tags.append("vintage")
    if year and year >= 2010:
        tags.append("modern")

    # Attributes — everything useful
    attributes: dict[str, Any] = {}
    for key in (
        "hp",
        "supertype",
        "subtypes",
        "types",
        "abilities",
        "attacks",
        "weaknesses",
        "resistances",
        "retreatCost",
        "convertedRetreatCost",
        "evolvesFrom",
        "evolvesTo",
        "rules",
        "regulationMark",
        "nationalPokedexNumbers",
        "artist",
        "flavorText",
        "legalities",
    ):
        val = card.get(key)
        if val not in (None, "", [], {}):
            attributes[key] = val
    tcgp = card.get("tcgplayer") or {}
    if tcgp.get("url"):
        attributes["tcgplayer_url"] = tcgp["url"]

    # Images
    image_set = {
        "small": _img(images.get("small"), alt=name),
        "normal": _img(images.get("large") or images.get("small"), alt=name),
        "large": _img(images.get("large"), alt=name),
        "art_crop": None,
    }

    pricing = _pokemon_pricing(card)

    set_data = {
        "id": f"pokemontcg:{set_obj.get('id')}" if set_obj.get("id") else None,
        "code": set_obj.get("id") or set_obj.get("ptcgoCode"),
        "name": set_obj.get("name"),
        "series": set_obj.get("series"),
        "release_date": set_obj.get("releaseDate"),
        "printed_total": set_obj.get("printedTotal"),
        "total_cards": set_obj.get("total"),
        "logo": _img(set_images.get("logo")),
        "symbol": _img(set_images.get("symbol")),
    }

    base_image = images.get("small") or images.get("large")
    return {
        "id": f"pokemontcg:{card.get('id')}",
        "name": name,
        "tcg": "pokemon",
        "set_name": set_obj.get("name"),
        "set_code": set_obj.get("id") or set_obj.get("ptcgoCode"),
        "number": card.get("number"),
        "rarity": card.get("rarity"),
        "image_url": base_image,
        "images": image_set,
        "year": year,
        "source": "pokemontcg",
        "attributes": attributes,
        "pricing_summary": pricing,
        "set": set_data,
        "tags": tags,
        "metadata": _meta("pokemontcg"),
    }


def _pokemon_pricing(card: dict[str, Any]) -> dict[str, Any] | None:
    """Extract TCGPlayer (preferred) or Cardmarket pricing into PricingSummary."""
    tcgp = card.get("tcgplayer") or {}
    prices = tcgp.get("prices") or {}
    chosen: dict[str, Any] | None = None
    for variant in (
        "holofoil",
        "reverseHolofoil",
        "1stEditionHolofoil",
        "normal",
        "1stEdition",
        "unlimited",
        "unlimitedHolofoil",
    ):
        v = prices.get(variant)
        if isinstance(v, dict) and (v.get("market") or v.get("mid")):
            chosen = v
            break
    if chosen:
        return {
            "card_id": f"pokemontcg:{card.get('id')}",
            "currency": "USD",
            "market": _money(chosen.get("market") or chosen.get("mid")),
            "low": _money(chosen.get("low")),
            "mid": _money(chosen.get("mid")),
            "high": _money(chosen.get("high")),
            "as_of": tcgp.get("updatedAt"),
            "sample_size": 1,
            "sources": ["tcgplayer"],
        }
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    avg = cm.get("averageSellPrice") or cm.get("trendPrice")
    if avg:
        return {
            "card_id": f"pokemontcg:{card.get('id')}",
            "currency": "EUR",
            "market": _money(avg),
            "low": _money(cm.get("lowPrice")),
            "mid": None,
            "high": None,
            "as_of": (card.get("cardmarket") or {}).get("updatedAt"),
            "sample_size": 1,
            "sources": ["cardmarket"],
        }
    return None


def _from_scryfall(card: dict[str, Any]) -> dict[str, Any]:
    image_uris = card.get("image_uris") or {}
    if not image_uris and card.get("card_faces"):
        faces = card["card_faces"]
        if faces and isinstance(faces[0], dict):
            image_uris = faces[0].get("image_uris") or {}

    name = card.get("name") or ""
    tags: list[str] = []
    if card.get("foil"):
        tags.append("foil")
    if card.get("reserved"):
        tags.append("reserved")
    if card.get("promo"):
        tags.append("promo")
    if card.get("reprint"):
        tags.append("reprint")
    year = _year(card.get("released_at"))
    if year and year < 2000:
        tags.append("vintage")
    if year and year >= 2010:
        tags.append("modern")

    attributes: dict[str, Any] = {}
    for key in (
        "mana_cost",
        "cmc",
        "type_line",
        "oracle_text",
        "power",
        "toughness",
        "loyalty",
        "colors",
        "color_identity",
        "keywords",
        "legalities",
        "reserved",
        "foil",
        "nonfoil",
        "oversized",
        "promo",
        "reprint",
        "variation",
        "frame",
        "frame_effects",
        "border_color",
        "artist",
        "flavor_text",
        "released_at",
        "prints_search_uri",
        "scryfall_uri",
        "edhrec_rank",
    ):
        val = card.get(key)
        if val not in (None, "", [], {}):
            attributes[key] = val

    image_set = {
        "small": _img(image_uris.get("small"), alt=name),
        "normal": _img(image_uris.get("normal"), alt=name),
        "large": _img(image_uris.get("large"), alt=name),
        "art_crop": _img(image_uris.get("art_crop"), alt=name),
    }

    prices = card.get("prices") or {}
    usd = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")
    pricing: dict[str, Any] | None = None
    if usd:
        market = _money(usd)
        if market:
            pricing = {
                "card_id": f"scryfall:{card.get('id')}",
                "currency": "USD",
                "market": market,
                "low": None,
                "mid": None,
                "high": None,
                "as_of": None,
                "sample_size": 1,
                "sources": ["scryfall"],
            }

    set_data = {
        "id": f"scryfall:{card.get('set_id')}" if card.get("set_id") else None,
        "code": card.get("set"),
        "name": card.get("set_name"),
        "series": card.get("set_type"),
        "release_date": card.get("released_at"),
        "printed_total": None,
        "total_cards": None,
        "logo": None,
        "symbol": None,
    }

    return {
        "id": f"scryfall:{card.get('id')}",
        "name": name,
        "tcg": "magic",
        "set_name": card.get("set_name"),
        "set_code": card.get("set"),
        "number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "image_url": image_uris.get("normal") or image_uris.get("small"),
        "images": image_set,
        "year": year,
        "source": "scryfall",
        "attributes": attributes,
        "pricing_summary": pricing,
        "set": set_data,
        "tags": tags,
        "metadata": _meta("scryfall"),
    }


def _from_yugioh(card: dict[str, Any]) -> dict[str, Any]:
    images = (card.get("card_images") or [{}])[0]
    sets = card.get("card_sets") or [{}]
    first_set = sets[0] if sets else {}
    name = card.get("name") or ""

    attributes: dict[str, Any] = {}
    for key in (
        "type",
        "frameType",
        "desc",
        "race",
        "archetype",
        "atk",
        "def",
        "level",
        "attribute",
        "scale",
        "linkval",
        "linkmarkers",
        "card_sets",
        "banlist_info",
        "misc_info",
    ):
        val = card.get(key)
        if val not in (None, "", [], {}):
            attributes[key] = val

    image_set = {
        "small": _img(images.get("image_url_small"), alt=name),
        "normal": _img(images.get("image_url"), alt=name),
        "large": _img(images.get("image_url"), alt=name),
        "art_crop": _img(images.get("image_url_cropped"), alt=name),
    }

    prices_list = card.get("card_prices") or []
    pricing: dict[str, Any] | None = None
    if prices_list:
        first = prices_list[0]
        numeric: list[float] = []
        for key in (
            "tcgplayer_price",
            "cardmarket_price",
            "ebay_price",
            "amazon_price",
        ):
            raw = first.get(key)
            try:
                v = float(raw)
                if v > 0:
                    numeric.append(v)
            except (TypeError, ValueError):
                continue
        if numeric:
            avg = sum(numeric) / len(numeric)
            pricing = {
                "card_id": f"ygoprodeck:{card.get('id')}",
                "currency": "USD",
                "market": _money(avg),
                "low": _money(min(numeric)),
                "mid": None,
                "high": _money(max(numeric)),
                "as_of": None,
                "sample_size": len(numeric),
                "sources": ["ygoprodeck"],
            }

    set_data = {
        "id": f"ygoprodeck:{first_set.get('set_code')}"
        if first_set.get("set_code")
        else None,
        "code": first_set.get("set_code"),
        "name": first_set.get("set_name"),
        "series": None,
        "release_date": None,
        "printed_total": None,
        "total_cards": None,
        "logo": None,
        "symbol": None,
    }

    tags: list[str] = []
    rarity = first_set.get("set_rarity") or ""
    if "ultra" in rarity.lower() or "secret" in rarity.lower():
        tags.append("rare")

    return {
        "id": f"ygoprodeck:{card.get('id')}",
        "name": name,
        "tcg": "yugioh",
        "set_name": first_set.get("set_name"),
        "set_code": first_set.get("set_code"),
        "number": first_set.get("set_code"),
        "rarity": first_set.get("set_rarity"),
        "image_url": images.get("image_url_small") or images.get("image_url"),
        "images": image_set,
        "year": None,
        "source": "ygoprodeck",
        "attributes": attributes,
        "pricing_summary": pricing,
        "set": set_data,
        "tags": tags,
        "metadata": _meta("ygoprodeck"),
    }


# ------------------------------------------------------------------- caching


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
        await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("cache_set failed: %s", exc)


# -------------------------------------------------------------- per-provider


# Pokémon TCG (Lucene-style) does NOT honour `*` inside quotes — quoted
# strings are treated as exact phrases, so `name:"pikachu*"` matches
# literally nothing. Build a token list that uses prefix wildcards on every
# whitespace-separated word, with special chars stripped so we never break
# the query parser. Falls back to a substring match if the cleaned query is
# empty (e.g. user typed only punctuation).
_POKEMON_SAFE_RE = re.compile(r"[^A-Za-z0-9\- ]+")


def _build_pokemon_query(q: str) -> str:
    cleaned = _POKEMON_SAFE_RE.sub(" ", q).strip().lower()
    if not cleaned:
        return f'name:"{q}"'
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return f'name:"{q}"'
    return " ".join(f"name:{tok}*" for tok in tokens)


async def _search_pokemon(q: str, limit: int) -> list[dict[str, Any]]:
    raw = await pokemon_tcg.search_cards(_build_pokemon_query(q), page=1, page_size=limit)
    return [_from_pokemon(c) for c in (raw.get("data") or [])][:limit]


def _bare_number(number: str | None) -> str | None:
    """Strip a collector number to its bare left-hand digits.

    ``"58/102"`` → ``"58"``, ``"008/165"`` → ``"8"``. Returns ``None``
    when there's nothing usable to pin a search on.
    """
    if not number:
        return None
    left = str(number).split("/", 1)[0].strip().lstrip("0")
    return left if left.isdigit() else None


async def search_cards_precise(
    *,
    tcg: str,
    name: str,
    number: str | None = None,
    set_code: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Field-aware catalog lookup for the identification pipeline.

    Unlike :func:`search_cards` (title-only fuzzy search), this pins the
    *collector number* — and the set when known — so the exact printing
    surfaces even when a name-only search buries it under promos. The
    canonical example: a name-only search for "Pikachu" never returns
    Base Set #58 in the top 10, but ``name:pikachu* number:58`` returns
    it precisely.

    Returns a list of unified card dicts (possibly empty). Never raises —
    upstream failures degrade to an empty list so the pipeline keeps the
    name-only candidates it already has.
    """
    name = (name or "").strip()
    if not name:
        return []
    tcg = (tcg or "all").lower()
    bare = _bare_number(number)
    if not bare:
        # Nothing to pin on — the name-only path already covers this.
        return []
    try:
        if tcg == "pokemon":
            tokens = [
                t for t in _POKEMON_SAFE_RE.sub(" ", name).strip().lower().split() if t
            ]
            if not tokens:
                return []
            parts = [f"name:{tok}*" for tok in tokens]
            parts.append(f"number:{bare}")
            raw = await pokemon_tcg.search_cards(
                " ".join(parts), page=1, page_size=limit
            )
            cards = [_from_pokemon(c) for c in (raw.get("data") or [])]
            if cards:
                return cards[:limit]
            # Fallback — the name wildcard matched nothing, almost always
            # because OCR garbled a character ("Gyarados" → "Gyarado5",
            # so name:gyarado5* hits zero). The collector number is a
            # strong key on its own: pull every printing with that number
            # and let name-similarity + HP/year scoring pick the right one
            # downstream. Widen the page so the correct set is in the pool.
            raw = await pokemon_tcg.search_cards(
                f"number:{bare}", page=1, page_size=max(limit, 30)
            )
            return [_from_pokemon(c) for c in (raw.get("data") or [])]
        if tcg == "magic":
            q_parts = [name]
            q_parts.append(f"cn:{number}")
            if set_code:
                q_parts.append(f"set:{set_code}")
            raw = await scryfall.search_cards(" ".join(q_parts), page=1)
            return [_from_scryfall(c) for c in (raw.get("data") or [])][:limit]
        # YGOPRODeck has no clean collector-number filter on the name
        # endpoint, so precise lookups don't help there — fall through.
    except (httpx.HTTPError, CircuitOpenError, ValueError, KeyError) as exc:
        logger.info("precise search failed (%s/%s #%s): %s", tcg, name, bare, exc)
    return []


async def _search_scryfall(q: str, limit: int) -> list[dict[str, Any]]:
    raw = await scryfall.search_cards(q, page=1)
    return [_from_scryfall(c) for c in (raw.get("data") or [])][:limit]


async def _search_ygoprodeck(q: str, limit: int) -> list[dict[str, Any]]:
    raw = await ygoprodeck.search_cards(q)
    return [_from_yugioh(c) for c in (raw.get("data") or [])[:limit]]


def _interleave(lists: list[list[dict[str, Any]]], cap: int) -> list[dict[str, Any]]:
    """Round-robin merge with a cap. Preserves provider variety."""
    out: list[dict[str, Any]] = []
    if not lists:
        return out
    max_len = max(len(lst) for lst in lists) if lists else 0
    for i in range(max_len):
        for lst in lists:
            if i < len(lst):
                out.append(lst[i])
                if len(out) >= cap:
                    return out
    return out


# -------------------------------------------------------------------- search


async def search_cards(q: str, tcg: str, limit: int) -> dict[str, Any]:
    """Search live upstream catalog and return a unified envelope."""
    q = (q or "").strip()
    tcg = (tcg or "all").lower()
    limit = _cap(limit)
    if not q:
        return _empty(tcg)

    if tcg in UNSUPPORTED_TCGS:
        return _empty(tcg, error="provider_not_configured")

    cache_key = f"loupe:cards:search:{tcg}:{q.lower()}:{limit}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        if tcg == "pokemon":
            items = await _search_pokemon(q, limit)
            body = {"results": items, "total": len(items), "source": "pokemontcg"}
        elif tcg == "yugioh":
            items = await _search_ygoprodeck(q, limit)
            body = {"results": items, "total": len(items), "source": "ygoprodeck"}
        elif tcg == "magic":
            items = await _search_scryfall(q, limit)
            body = {"results": items, "total": len(items), "source": "scryfall"}
        else:  # "all" → parallel fan-out with early-settle
            per = max(3, min(limit, MAX_LIMIT))

            # Kick off all three providers as named tasks so we can tell
            # which one returned what after `asyncio.wait` resolves.
            provider_labels = ("pokemontcg", "scryfall", "ygoprodeck")
            tasks: dict[str, asyncio.Task[list[dict[str, Any]]]] = {
                "pokemontcg": asyncio.create_task(_search_pokemon(q, per)),
                "scryfall": asyncio.create_task(_search_scryfall(q, per)),
                "ygoprodeck": asyncio.create_task(_search_ygoprodeck(q, per)),
            }

            # Phase 1: "fast settle". Wait up to FAST_SETTLE_DEADLINE.
            # If at least two providers have returned (success or failure)
            # we ship what we have and cancel the slowpoke. This is the
            # main perceived-latency win: one chronically-slow upstream
            # no longer blocks the entire response.
            await asyncio.wait(
                tasks.values(),
                timeout=FAST_SETTLE_DEADLINE,
                return_when=asyncio.ALL_COMPLETED,
            )
            done_count = sum(1 for t in tasks.values() if t.done())

            # Phase 2: if we don't have enough yet, wait the rest of the
            # per-provider budget for the stragglers.
            if done_count < 2:
                remaining = [t for t in tasks.values() if not t.done()]
                if remaining:
                    await asyncio.wait(
                        remaining,
                        timeout=max(
                            0.0, PER_PROVIDER_TIMEOUT - FAST_SETTLE_DEADLINE
                        ),
                        return_when=asyncio.ALL_COMPLETED,
                    )

            # Cancel anything still running and collect results.
            lists: list[list[dict[str, Any]]] = []
            any_failed = False
            for label in provider_labels:
                task = tasks[label]
                if not task.done():
                    task.cancel()
                    any_failed = True
                    logger.info(
                        "multi-search upstream %s cancelled (slow > %ss)",
                        label,
                        PER_PROVIDER_TIMEOUT,
                    )
                    continue
                try:
                    res = task.result()
                except (asyncio.CancelledError, Exception) as exc:
                    any_failed = True
                    logger.warning(
                        "multi-search upstream %s failed: %s", label, exc
                    )
                    continue
                if isinstance(res, list):
                    lists.append(res)
            merged = _interleave(lists, limit)
            body = {
                "results": merged,
                "total": len(merged),
                "source": "mixed",
            }
            if any_failed:
                # Mark partial so the cache layer below uses the short TTL.
                # Without this, a single slow upstream blip would silently
                # hide an entire TCG's results for the full CARD_SEARCH_TTL
                # (5 min) — e.g. searching "raichu" returns only Magic
                # cards because the cached partial response is reused on
                # every retry.
                body["partial"] = True
    except (httpx.HTTPError, CircuitOpenError) as exc:
        logger.warning("upstream search failed (%s): %s", tcg, exc)
        return _empty(tcg, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected error in search_cards: %s", exc)
        return _empty(tcg, error="upstream error")

    # Don't poison the cache with partial fan-out results. If pokemontcg
    # (or any provider) timed out, write a much shorter TTL so the next
    # keystroke can pick up a recovered provider instead of being stuck
    # showing only the surviving TCGs for the next 5 minutes.
    ttl = PARTIAL_RESULT_TTL if body.get("partial") else CARD_SEARCH_TTL
    await _cache_set(cache_key, body, ttl)
    return body


# --------------------------------------------------------------------- single


async def resolve_pricing_for_local(
    *,
    tcg: str,
    name: str,
    set_code: str | None,
    number: str | None,
) -> dict[str, Any] | None:
    """Look up a locally-seeded card on the matching upstream provider and
    return the unified card dict (including ``pricing_summary``), or ``None``
    if no confident match is found.

    Used to retroactively enrich UUID-only catalog rows with the free pricing
    embedded in Pokémon TCG / Scryfall / YGOPRODeck responses.
    """
    name = (name or "").strip()
    if not name:
        return None
    try:
        if tcg == "pokemon":
            bare_number = number.split("/", 1)[0].strip() if number else None
            # Try progressively-looser queries. Locally-seeded cards
            # often have set codes that don't match pokemontcg.io's
            # ``set.id`` slugs (e.g. "BS" vs. "base1"), and the
            # "8/102" collector-form throws off ``number:`` matching
            # when paired with the wrong set. Always start strict so a
            # well-formed row resolves precisely; fall back to
            # name+number, then name-only, so a mis-formatted seed
            # row still finds today's market price instead of poisoning
            # the negative cache for 5 minutes.
            attempts: list[list[str]] = []
            strict = [f'name:"{name}"']
            if set_code:
                strict.append(f"set.id:{set_code}")
            if bare_number:
                strict.append(f"number:{bare_number}")
            attempts.append(strict)
            if set_code and bare_number:
                attempts.append([f'name:"{name}"', f"number:{bare_number}"])
            attempts.append([f'name:"{name}"'])
            seen: set[str] = set()
            for q_parts in attempts:
                q = " ".join(q_parts)
                if q in seen:
                    continue
                seen.add(q)
                # Per-attempt budget: a relaxed query (e.g. plain
                # ``name:"Blastoise"``) can be slow upstream, and we'd
                # rather skip it than blow the user's 4s outer budget.
                try:
                    body = await asyncio.wait_for(
                        pokemon_tcg.search_cards(q, page=1, page_size=1),
                        timeout=2.5,
                    )
                except (TimeoutError, httpx.HTTPError, CircuitOpenError):
                    continue
                data = (body.get("data") or []) if isinstance(body, dict) else []
                if data:
                    return _from_pokemon(data[0])
        elif tcg == "magic":
            q_parts = [f'!"{name}"']
            if set_code:
                q_parts.append(f"set:{set_code}")
            if number:
                q_parts.append(f"cn:{number}")
            body = await scryfall.search_cards(" ".join(q_parts), page=1)
            data = (body.get("data") or []) if isinstance(body, dict) else []
            if data:
                return _from_scryfall(data[0])
        elif tcg == "yugioh":
            body = await ygoprodeck.search_cards(name)
            data = (body.get("data") or []) if isinstance(body, dict) else []
            if data:
                return _from_yugioh(data[0])
    except (httpx.HTTPError, CircuitOpenError, ValueError, KeyError) as exc:
        logger.info("resolve_pricing_for_local(%s/%s) failed: %s", tcg, name, exc)
        return None
    return None


async def get_card(card_id: str) -> dict[str, Any] | None:
    """Look up a single card by composite ``<source>:<upstream_id>`` ID.

    Falls back to a local-DB lookup when given a UUID. On first view of a
    locally-seeded card we transparently resolve it on the matching upstream
    and persist the embedded pricing into ``Card.card_metadata`` so future
    requests are served from the DB without re-hitting upstream.
    """
    if ":" not in card_id:
        # UUID fallback: resolve against the local catalog.
        try:
            import uuid as _uuid

            as_uuid = _uuid.UUID(card_id)
        except ValueError:
            return None
        from sqlalchemy import select as _select

        from app.db.session import get_sessionmaker
        from app.models.card_external_ref import CardExternalRef
        from app.services.catalog import card_catalog_service

        maker = get_sessionmaker()
        async with maker() as session:
            row = await card_catalog_service.get_card(session, as_uuid)
            if row is None:
                return None
            card_set = getattr(row, "card_set", None)
            set_name = card_set.name if card_set is not None else None
            set_code = card_set.code if card_set is not None else None
            year = row.year
            if (
                year is None
                and card_set is not None
                and card_set.release_date is not None
            ):
                year = card_set.release_date.year
            tcg_str = row.tcg.value if hasattr(row.tcg, "value") else str(row.tcg)

            meta = row.card_metadata if isinstance(row.card_metadata, dict) else {}
            cached_pricing = meta.get("pricing_summary") if meta else None
            cached_image_url = meta.get("image_url") if meta else None
            cached_images = meta.get("images") if meta else None

            # If we have nothing cached, try once to resolve upstream and
            # persist whatever we get back. Failures are non-fatal.
            #
            # NEGATIVE CACHE: when an upstream resolve times out or errors
            # we record a short-lived marker so the next request for the
            # same UUID doesn't pay the 4s timeout penalty again. Without
            # this, opening a single card detail screen fires 6+ parallel
            # routes (canonical/market/comps/listings/…) and each one
            # individually waits the full 4s on the dead provider, turning
            # a 4s blip into a 24s page load.
            neg_cache_key = f"loupe:resolve_neg:{as_uuid}"
            if not cached_pricing and await _cache_get(neg_cache_key) is not None:
                # Skip the upstream resolve entirely and serve what we
                # have from the local DB. Negative TTL is 5 min.
                pass
            elif not cached_pricing:
                resolved = None
                upstream_match: tuple[str, str] | None = None

                async def _do_resolve() -> tuple[
                    dict[str, Any] | None, tuple[str, str] | None
                ]:
                    """Inner resolver — returns (card_dict, upstream_ref).

                    Wrapped in a hard timeout below so card detail
                    responses never block the UI for >4s waiting on a
                    sluggish upstream (Pokémon TCG, Scryfall, etc.).
                    """
                    local_resolved: dict[str, Any] | None = None
                    local_match: tuple[str, str] | None = None

                    # Cheap path: if we already linked an upstream ref,
                    # use it directly instead of searching by name.
                    ref_rows = (
                        (
                            await session.execute(
                                _select(CardExternalRef).where(
                                    CardExternalRef.card_id == as_uuid
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    # NOTE: pokemontcg.io was 404-ing for valid queries
                    # in May 2026; demoted to last-resort so canonical
                    # resolves prefer the healthier scryfall /
                    # ygoprodeck mirrors when a card has refs on both.
                    priority = {"scryfall": 0, "ygoprodeck": 1, "pokemontcg": 2}
                    preferred = sorted(
                        ref_rows, key=lambda r: priority.get(r.source, 99)
                    )
                    for ref in preferred:
                        if ref.source not in priority:
                            continue
                        local_resolved = await get_card(
                            f"{ref.source}:{ref.external_id}"
                        )
                        if local_resolved is not None and local_resolved.get(
                            "pricing_summary"
                        ):
                            local_match = (ref.source, ref.external_id)
                            break

                    # Expensive path: search upstream by name.
                    if local_resolved is None or not local_resolved.get(
                        "pricing_summary"
                    ):
                        local_resolved = await resolve_pricing_for_local(
                            tcg=tcg_str,
                            name=row.name,
                            set_code=set_code,
                            number=row.number,
                        )
                        if local_resolved is not None and local_resolved.get("id"):
                            rid = local_resolved["id"]
                            if ":" in rid:
                                src, _, ext = rid.partition(":")
                                local_match = (src, ext)
                    return local_resolved, local_match

                try:
                    resolved, upstream_match = await asyncio.wait_for(
                        _do_resolve(), timeout=4.0
                    )
                except TimeoutError:
                    logger.info(
                        "upstream resolve skipped for %s (timeout after 4s)", as_uuid
                    )
                    resolved = None
                    upstream_match = None
                    await _cache_set(neg_cache_key, {"reason": "timeout"}, 300)
                except Exception as exc:
                    # Use %r so the type+args are always visible — plain %s
                    # on many provider exceptions renders as empty.
                    logger.info("upstream resolve skipped for %s (%r)", as_uuid, exc)
                    resolved = None
                    upstream_match = None
                    await _cache_set(neg_cache_key, {"reason": "error"}, 300)

                if resolved is None:
                    # Successful-but-empty resolves get a SHORT TTL so a
                    # mis-formatted seed row (e.g. set_code "BS" vs the
                    # upstream's "base1") can be retried after the next
                    # pull-to-refresh instead of staying blank for 5
                    # minutes. Real upstream errors keep the longer 300s
                    # TTL (set above) so we don't hammer a flaky API.
                    await _cache_set(neg_cache_key, {"reason": "empty"}, 30)

                if resolved is not None:
                    cached_pricing = resolved.get("pricing_summary")
                    cached_image_url = resolved.get("image_url") or cached_image_url
                    cached_images = resolved.get("images") or cached_images
                    new_meta = dict(meta)
                    if cached_pricing:
                        new_meta["pricing_summary"] = cached_pricing
                    if cached_image_url:
                        new_meta["image_url"] = cached_image_url
                    if cached_images:
                        new_meta["images"] = cached_images
                    row.card_metadata = new_meta

                    # Persist the discovered upstream link so we never have to
                    # search by name for this card again.
                    if upstream_match is not None:
                        from app.services.catalog import card_resolver_service

                        await card_resolver_service.link_external_ref(
                            session,
                            card_id=as_uuid,
                            source=upstream_match[0],
                            external_id=upstream_match[1],
                            confidence=0.9,
                        )
                    try:
                        await session.commit()
                    except Exception as exc:  # pragma: no cover - best effort
                        logger.debug("persist resolved pricing failed: %s", exc)
                        await session.rollback()

        return {
            "id": card_id,
            "name": row.name,
            "tcg": tcg_str,
            "set_name": set_name,
            "set_code": set_code,
            "number": row.number,
            "rarity": row.rarity,
            "image_url": cached_image_url or row.image_url,
            "images": cached_images,
            "year": year,
            "source": "loupe-db",
            "pricing_summary": cached_pricing,
        }
    source, _, upstream_id = card_id.partition(":")
    source = source.lower()
    if not upstream_id:
        return None

    cache_key = f"loupe:cards:item:{source}:{upstream_id}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached
    # Long-lived "last-known-good" cache. Survives normal TTL expiry so a
    # transient upstream blip (network reset, 5xx) can serve a slightly
    # stale card body instead of returning 404 to the UI.
    stale_key = f"loupe:cards:item_lkg:{source}:{upstream_id}"

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
    except (httpx.HTTPError, CircuitOpenError) as exc:
        logger.warning("upstream get_card failed (%s): %s", source, exc)
        # Fallback 1: serve the last-known-good copy if we have one.
        stale = await _cache_get(stale_key)
        if stale is not None:
            return stale
        # Fallback 2: build a minimal response from the local Card row
        # linked by CardExternalRef. Better to show a card with no
        # pricing than to 404 the detail page on a network blip.
        return await _local_card_from_external_ref(source, upstream_id)

    if result is not None:
        await _cache_set(cache_key, result, CARD_DETAIL_TTL)
        # Mirror to the LKG cache with a much longer TTL (24h). Worst
        # case the UI shows a day-old name/image while pricing endpoints
        # serve fresh numbers on their own.
        await _cache_set(stale_key, result, 86400)
    else:
        # Upstream returned nothing — try the local DB before giving up.
        local = await _local_card_from_external_ref(source, upstream_id)
        if local is not None:
            return local
    return result


async def _local_card_from_external_ref(
    source: str, upstream_id: str
) -> dict[str, Any] | None:
    """Build a minimal card payload from the local DB by external ref.

    Used as a fallback when an upstream provider is unreachable so the
    card detail page degrades gracefully instead of returning 404.
    """
    from sqlalchemy import select as _select

    from app.db.session import get_sessionmaker
    from app.models.card_external_ref import CardExternalRef
    from app.services.catalog import card_catalog_service

    try:
        maker = get_sessionmaker()
        async with maker() as session:
            ref = (
                await session.execute(
                    _select(CardExternalRef).where(
                        CardExternalRef.source == source,
                        CardExternalRef.external_id == upstream_id,
                    )
                )
            ).scalar_one_or_none()
            if ref is None:
                return None
            row = await card_catalog_service.get_card(session, ref.card_id)
            if row is None:
                return None
            card_set = getattr(row, "card_set", None)
            meta = row.card_metadata if isinstance(row.card_metadata, dict) else {}
            tcg_str = row.tcg.value if hasattr(row.tcg, "value") else str(row.tcg)
            return {
                "id": f"{source}:{upstream_id}",
                "name": row.name,
                "tcg": tcg_str,
                "set_name": card_set.name if card_set is not None else None,
                "set_code": card_set.code if card_set is not None else None,
                "number": row.number,
                "rarity": row.rarity,
                "image_url": meta.get("image_url") or row.image_url,
                "images": meta.get("images"),
                "year": row.year,
                "source": "loupe-db",
                "pricing_summary": meta.get("pricing_summary"),
            }
    except Exception as exc:  # pragma: no cover - best effort fallback
        logger.debug("local fallback for %s:%s failed: %s", source, upstream_id, exc)
        return None


# ----------------------------------------------------------- price history


_RANGE_DAYS = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "180d": 180,
    "1y": 365,
    "365d": 365,
    "all": 730,
}


def _granularity(days: int) -> str:
    if days <= 30:
        return "daily"
    if days <= 180:
        return "weekly"
    return "monthly"


def _step_days(days: int) -> int:
    if days <= 90:
        return 1
    return 7


async def get_price_history(
    card_id: str,
    range_: str = "30d",
    house: str = "raw",
    grade: str | None = None,
) -> dict[str, Any] | None:
    """Return a stable, shape-correct synthesized price series.

    Until we have a real historical-prices upstream, we walk a deterministic
    random walk around the current ``pricing_summary.market`` so the chart
    is steady across refreshes (seeded by card id).

    When ``house``/``grade`` are supplied (e.g. ``house="psa"``, ``grade="10"``)
    the series is scaled by the same ``_HOUSE_DRIFT`` × grade-multiplier
    math that produces the per-house table on the card-detail screen, so
    tapping a grade row filters the chart to that specific tier instead
    of showing the raw market line.
    """
    range_ = (range_ or "30d").lower()
    days = _RANGE_DAYS.get(range_, 30)

    house_key = (house or "raw").lower()
    grade_key = (grade or "").strip()
    cache_key = (
        f"loupe:cards:prices:{card_id}:{range_}:{house_key}:{grade_key or 'all'}"
    )
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    card = await get_card(card_id)
    if card is None:
        return None

    pricing = card.get("pricing_summary") or {}
    market_obj = pricing.get("market") if pricing else None
    market_amt = market_obj.get("amount") if isinstance(market_obj, dict) else None

    body: dict[str, Any]
    if not market_amt:
        body = {
            "card_id": card_id,
            "currency": (pricing.get("currency") if pricing else None) or "USD",
            "points": [],
            "granularity": _granularity(days),
            "range": range_,
            "house": house_key,
            "grade": grade_key or None,
            "summary": {
                "min": None,
                "max": None,
                "avg": None,
                "current": None,
                "change_pct": None,
                "n_points": 0,
            },
        }
        await _cache_set(cache_key, body, PRICE_HISTORY_TTL)
        return body

    # Seed the walk with house+grade so each filtered series gets its
    # own deterministic shape (otherwise tapping PSA 10 would just
    # rescale the raw walk and look identical).
    seed_key = hashlib.sha256(
        f"{card_id}|{house_key}|{grade_key}".encode()
    ).hexdigest()
    seed = int(seed_key, 16) % (2**32)
    rng = random.Random(seed)
    step = _step_days(days)
    n_points = max(2, days // step)

    raw_base = float(market_amt)
    base = _scaled_base(card, raw_base, house_key, grade_key, rng)
    drift = (rng.random() - 0.5) * 0.20  # overall trend up/down ±10%

    end_dt = utcnow()
    start_dt = end_dt - timedelta(days=days)

    walk = [0.0]
    for _ in range(n_points - 1):
        walk.append(walk[-1] + (rng.random() - 0.5))
    w_min, w_max = min(walk), max(walk)
    spread = (w_max - w_min) or 1.0
    normalised = [((w - w_min) / spread - 0.5) * 0.30 for w in walk]  # ±15%

    sources = pricing.get("sources") or ["synthetic"]
    source_label = sources[0] if sources else "synthetic"

    values: list[float] = []
    points: list[dict[str, Any]] = []
    for i, raw in enumerate(normalised):
        t = i / max(1, n_points - 1)
        price = base * (1.0 + drift * (1.0 - t)) * (1.0 + raw)
        if i == n_points - 1:
            price = base  # pin the tail to the live (house, grade) price
        price = round(max(0.01, price), 2)
        values.append(price)
        ts = (start_dt + timedelta(days=i * step)).isoformat()
        points.append(
            {
                "ts": ts,
                "price": price,
                "currency": "USD",
                "source": source_label,
            }
        )

    pmin = min(values)
    pmax = max(values)
    pavg = round(sum(values) / len(values), 2)
    change_pct = (
        round(((values[-1] - values[0]) / values[0]) * 100.0, 2) if values[0] else None
    )

    body = {
        "card_id": card_id,
        "currency": "USD",
        "points": points,
        "granularity": _granularity(days),
        "range": range_,
        "house": house_key,
        "grade": grade_key or None,
        "summary": {
            "min": pmin,
            "max": pmax,
            "avg": pavg,
            "current": values[-1],
            "change_pct": change_pct,
            "n_points": len(points),
        },
    }
    await _cache_set(cache_key, body, PRICE_HISTORY_TTL)
    return body


# ---- house/grade scaling (mirrors market_service per-row math) ------

# Kept in-module to avoid an import cycle with market_service (which
# also pulls helpers from this module via its synthesizer chain).
_HOUSE_DRIFT_HISTORY: dict[str, float] = {
    "raw": 1.00,
    "psa": 1.00,
    "cgc": 0.95,
    "bgs": 1.05,
    "sgc": 0.92,
    "tag": 0.85,
}
_GRADE_MULT_HISTORY: dict[float, tuple[float, float]] = {
    10: (10.0, 18.0),
    9.5: (5.0, 8.0),
    9: (2.5, 4.0),
    8.5: (1.6, 2.2),
    8: (1.2, 1.6),
    7: (0.9, 1.1),
    6: (0.70, 0.78),
    5: (0.55, 0.62),
    4: (0.45, 0.52),
    3: (0.35, 0.40),
}


def _parse_grade(grade_key: str) -> float | None:
    """Accept ``"10"``, ``"9.5"``, ``"PSA 10"``, ``"10 BLACK"`` → float."""
    if not grade_key:
        return None
    s = grade_key.strip().lower()
    # Strip a leading house token like "psa 10".
    for h in ("psa", "cgc", "bgs", "sgc", "tag"):
        if s.startswith(h):
            s = s[len(h):].strip()
            break
    # Drop trailing labels like "black" on "10 BLACK".
    parts = s.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def _vintage_factor_year(year: int | None) -> float:
    if not year:
        return 0.4
    if year <= 1995:
        return 1.0
    if year >= 2020:
        return 0.0
    return max(0.0, min(1.0, (2020 - year) / 25.0))


def _scaled_base(
    card: dict[str, Any],
    raw_base: float,
    house_key: str,
    grade_key: str,
    rng: random.Random,
) -> float:
    """Apply house drift × grade multiplier to the raw market price."""
    if house_key == "raw" or not house_key:
        return raw_base
    drift = _HOUSE_DRIFT_HISTORY.get(house_key, 1.0)
    grade = _parse_grade(grade_key)
    if grade is None or grade not in _GRADE_MULT_HISTORY:
        # House supplied without a usable grade — just apply house drift.
        return raw_base * drift
    lo, hi = _GRADE_MULT_HISTORY[grade]
    vf = _vintage_factor_year(card.get("year"))
    # Mirror market_service._house_grade_row's blend so the chart's
    # current/tail value aligns with the table row the user tapped.
    mult = lo + (hi - lo) * (0.25 + 0.75 * vf) * (0.85 + 0.30 * rng.random())
    # BGS 10 BLACK premium — only when explicitly tapped.
    if house_key == "bgs" and "black" in grade_key.lower():
        mult *= 1.4 + rng.random() * 0.6
    return raw_base * mult * drift


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
    except (httpx.HTTPError, CircuitOpenError) as exc:
        logger.warning("upstream list_sets failed (%s): %s", tcg, exc)
        return {
            "results": [],
            "total": 0,
            "source": _source_for(tcg),
            "error": str(exc),
        }

    await _cache_set(cache_key, body, SET_LIST_TTL)
    return body


__all__ = ["get_card", "get_price_history", "list_sets", "search_cards"]
