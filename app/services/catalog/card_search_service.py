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
from datetime import date, timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.integrations._http import (
    apitcg,
    digimoncard,
    pokemon_tcg,
    scryfall,
    ygoprodeck,
)
from app.platform.api_budget import ApiBudget
from app.platform.cache_config import (
    CARD_DETAIL_TTL,
    CARD_SEARCH_TTL,
    PRICE_HISTORY_TTL,
    SET_LIST_TTL,
)
from app.platform.cache_swr import swr_get_or_refresh
from app.platform.circuit_breaker import CircuitOpenError
from app.platform.redis_client import get_redis
from app.schemas.unified_card import (
    UnifiedCard,
    UnifiedImage,
    UnifiedImageSet,
    UnifiedMetadata,
    UnifiedMoney,
    UnifiedPricingSummary,
    UnifiedSet,
)
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("services.card_search")

# Upper bound on results per search. Set generously so a popular name (e.g.
# "mewtwo" has 200+ printings) returns a deep, pageable set rather than a
# hard ~20-row cap. The storefront only requests this many for the full
# results page; the typeahead asks for a small top-N (see public_search).
MAX_LIMIT = 250
#: Hard ceiling per provider in the ``tcg=all`` fan-out. Healthy upstream
#: responses come back in 300-800 ms; this cap exists purely to keep one
#: misbehaving provider from holding the entire keystroke hostage. Kept
#: generous because the typeahead is non-blocking (it shows the previous
#: results while refetching), and the Pokemon catalog — the anchor with the
#: most hits — can run 3-4 s cold; cancelling it too early is what made
#: common queries like "mewtwo" intermittently return nothing.
#: Partial failures are NOT cached for long (see ``PARTIAL_RESULT_TTL``)
#: so a brief blip self-heals on the next keystroke.
PER_PROVIDER_TIMEOUT = 5.0

#: Soft deadline: once this elapses, if at least two providers have
#: already returned we ship the response and cancel the laggard.
#: This means a slow Pokemon TCG no longer blocks Magic+Yu-Gi-Oh
#: from rendering — the user sees something within ~1.5s instead of 3s+.
FAST_SETTLE_DEADLINE = 1.5

#: Cache TTL when one or more providers in a ``tcg=all`` fan-out failed.
#: Short enough that a recovered provider shows up on the next keystroke,
#: long enough that rapid retyping doesn't hammer the upstream.
PARTIAL_RESULT_TTL = 20

#: How long a *successful* (non-empty, complete) search is kept as a "last
#: good" fallback. The Pokemon TCG upstream has erratic latency (1-37 s), so a
#: cold fetch can time out and return zero rows; serving the last good result
#: instead means a query that has *ever* succeeded never looks broken again
#: within this window. Catalog identity is stable, so 6 h is safe.
LASTGOOD_RESULT_TTL = 6 * 60 * 60

#: Providers we *don't* have an upstream for yet — returned gracefully.
UNSUPPORTED_TCGS = {"lorcana", "sports"}


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
        "digimon": "digimoncard",
        "all": "mixed",
        "onepiece": "apitcg-onepiece",
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


def _img(url: str | None, alt: str | None = None) -> UnifiedImage | None:
    if not url:
        return None
    return UnifiedImage(url=url, width=None, height=None, alt=alt)


def _now_iso() -> str:
    return utcnow().isoformat()


def _meta(source: str) -> UnifiedMetadata:
    return UnifiedMetadata(source=source, last_synced_at=_now_iso(), confidence=1.0)


def _money(amount: Any) -> UnifiedMoney | None:
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return UnifiedMoney(amount=round(v, 2), currency="USD")


# ------------------------------------------------------------------ adapters


def _from_pokemon(card: dict[str, Any]) -> dict[str, Any]:
    """Map a Pokémon TCG API card to the unified rich shape (as a plain dict)."""
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
    image_set = UnifiedImageSet(
        small=_img(images.get("small"), alt=name),
        normal=_img(images.get("large") or images.get("small"), alt=name),
        large=_img(images.get("large"), alt=name),
        art_crop=None,
    )

    pricing = _pokemon_pricing(card)

    set_data = UnifiedSet(
        id=f"pokemontcg:{set_obj.get('id')}" if set_obj.get("id") else None,
        code=set_obj.get("id") or set_obj.get("ptcgoCode"),
        name=set_obj.get("name"),
        series=set_obj.get("series"),
        release_date=set_obj.get("releaseDate"),
        printed_total=set_obj.get("printedTotal"),
        total_cards=set_obj.get("total"),
        logo=_img(set_images.get("logo")),
        symbol=_img(set_images.get("symbol")),
    )

    base_image = images.get("small") or images.get("large")
    return UnifiedCard(
        id=f"pokemontcg:{card.get('id')}",
        name=name,
        tcg="pokemon",
        set_name=set_obj.get("name"),
        set_code=set_obj.get("id") or set_obj.get("ptcgoCode"),
        number=card.get("number"),
        rarity=card.get("rarity"),
        image_url=base_image,
        images=image_set,
        year=year,
        source="pokemontcg",
        attributes=attributes,
        pricing_summary=pricing,
        set=set_data,
        tags=tags,
        metadata=_meta("pokemontcg"),
    ).model_dump()


def _pokemon_pricing(card: dict[str, Any]) -> UnifiedPricingSummary | None:
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
        return UnifiedPricingSummary(
            card_id=f"pokemontcg:{card.get('id')}",
            currency="USD",
            market=_money(chosen.get("market") or chosen.get("mid")),
            low=_money(chosen.get("low")),
            mid=_money(chosen.get("mid")),
            high=_money(chosen.get("high")),
            as_of=tcgp.get("updatedAt"),
            sample_size=1,
            sources=["tcgplayer"],
        )
    cm = (card.get("cardmarket") or {}).get("prices") or {}
    avg = cm.get("averageSellPrice") or cm.get("trendPrice")
    if avg:
        return UnifiedPricingSummary(
            card_id=f"pokemontcg:{card.get('id')}",
            currency="EUR",
            market=_money(avg),
            low=_money(cm.get("lowPrice")),
            mid=None,
            high=None,
            as_of=(card.get("cardmarket") or {}).get("updatedAt"),
            sample_size=1,
            sources=["cardmarket"],
        )
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

    image_set = UnifiedImageSet(
        small=_img(image_uris.get("small"), alt=name),
        normal=_img(image_uris.get("normal"), alt=name),
        large=_img(image_uris.get("large"), alt=name),
        art_crop=_img(image_uris.get("art_crop"), alt=name),
    )

    prices = card.get("prices") or {}
    usd = prices.get("usd") or prices.get("usd_foil") or prices.get("usd_etched")
    pricing: UnifiedPricingSummary | None = None
    if usd:
        market = _money(usd)
        if market:
            pricing = UnifiedPricingSummary(
                card_id=f"scryfall:{card.get('id')}",
                currency="USD",
                market=market,
                low=None,
                mid=None,
                high=None,
                as_of=None,
                sample_size=1,
                sources=["scryfall"],
            )

    set_data = UnifiedSet(
        id=f"scryfall:{card.get('set_id')}" if card.get("set_id") else None,
        code=card.get("set"),
        name=card.get("set_name"),
        series=card.get("set_type"),
        release_date=card.get("released_at"),
        printed_total=None,
        total_cards=None,
        logo=None,
        symbol=None,
    )

    return UnifiedCard(
        id=f"scryfall:{card.get('id')}",
        name=name,
        tcg="magic",
        set_name=card.get("set_name"),
        set_code=card.get("set"),
        number=card.get("collector_number"),
        rarity=card.get("rarity"),
        image_url=image_uris.get("normal") or image_uris.get("small"),
        images=image_set,
        year=year,
        source="scryfall",
        attributes=attributes,
        pricing_summary=pricing,
        set=set_data,
        tags=tags,
        metadata=_meta("scryfall"),
    ).model_dump()


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

    image_set = UnifiedImageSet(
        small=_img(images.get("image_url_small"), alt=name),
        normal=_img(images.get("image_url"), alt=name),
        large=_img(images.get("image_url"), alt=name),
        art_crop=_img(images.get("image_url_cropped"), alt=name),
    )

    prices_list = card.get("card_prices") or []
    pricing: UnifiedPricingSummary | None = None
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
            pricing = UnifiedPricingSummary(
                card_id=f"ygoprodeck:{card.get('id')}",
                currency="USD",
                market=_money(avg),
                low=_money(min(numeric)),
                mid=None,
                high=_money(max(numeric)),
                as_of=None,
                sample_size=len(numeric),
                sources=["ygoprodeck"],
            )

    set_data = UnifiedSet(
        id=f"ygoprodeck:{first_set.get('set_code')}"
        if first_set.get("set_code")
        else None,
        code=first_set.get("set_code"),
        name=first_set.get("set_name"),
        series=None,
        release_date=None,
        printed_total=None,
        total_cards=None,
        logo=None,
        symbol=None,
    )

    tags: list[str] = []
    rarity = first_set.get("set_rarity") or ""
    if "ultra" in rarity.lower() or "secret" in rarity.lower():
        tags.append("rare")

    return UnifiedCard(
        id=f"ygoprodeck:{card.get('id')}",
        name=name,
        tcg="yugioh",
        set_name=first_set.get("set_name"),
        set_code=first_set.get("set_code"),
        number=first_set.get("set_code"),
        rarity=first_set.get("set_rarity"),
        image_url=images.get("image_url_small") or images.get("image_url"),
        images=image_set,
        year=None,
        source="ygoprodeck",
        attributes=attributes,
        pricing_summary=pricing,
        set=set_data,
        tags=tags,
        metadata=_meta("ygoprodeck"),
    ).model_dump()


def _from_digimon(card: dict[str, Any]) -> dict[str, Any]:
    """Map a digimoncard.io card to the unified rich shape.

    The printed id encodes the set: ``BT17-017`` → set ``BT17``; promo/starter
    ids without a dash (``P-009`` keeps ``P``) use the leading token. Art is not
    in the payload — it lives at a deterministic URL keyed by the id.
    """
    cid = str(card.get("id") or "")
    name = card.get("name") or ""
    # A card reprinted across sets can carry a list of set names — take the first.
    raw_set = card.get("set_name")
    set_name = (
        (raw_set[0] if raw_set else None) if isinstance(raw_set, list) else raw_set
    )
    set_code = cid.rsplit("-", 1)[0] if "-" in cid else cid
    img = digimoncard.image_url(cid)

    attributes: dict[str, Any] = {}
    for key in (
        "type",
        "color",
        "color2",
        "dp",
        "play_cost",
        "evolution_cost",
        "stage",
        "form",
        "attribute",
        "digi_type",
        "level",
        "main_effect",
        "source_effect",
        "series",
        "artist",
    ):
        val = card.get(key)
        if val not in (None, "", [], {}):
            attributes[key] = val

    image_set = UnifiedImageSet(
        small=_img(img, alt=name),
        normal=_img(img, alt=name),
        large=_img(img, alt=name),
        art_crop=None,
    )

    set_data = UnifiedSet(
        id=f"digimoncard:{set_code}" if set_code else None,
        code=set_code or None,
        name=set_name,
        series=card.get("series"),
        release_date=None,
        printed_total=None,
        total_cards=None,
        logo=None,
        symbol=None,
    )

    rarity = card.get("rarity")
    tags: list[str] = []
    if isinstance(rarity, str) and rarity.lower() in ("sr", "sec", "ssr"):
        tags.append("rare")

    return UnifiedCard(
        id=f"digimoncard:{cid}",
        name=name,
        tcg="digimon",
        set_name=set_name,
        set_code=set_code or None,
        number=cid,
        rarity=rarity,
        image_url=img,
        images=image_set,
        year=None,
        source="digimoncard",
        attributes=attributes,
        pricing_summary=None,
        set=set_data,
        tags=tags,
        metadata=_meta("digimoncard"),
    ).model_dump()


def _from_apitcg_onepiece(card: dict[str, Any]) -> dict[str, Any]:
    """Map an apitcg One Piece card to the unified rich shape.

    The id encodes the set (``OP03-070`` → ``OP03``, ``ST14-001`` → ``ST14``).
    apitcg carries no prices, so One Piece is catalog-only (price rails hide).
    """
    cid = str(card.get("id") or "")
    name = card.get("name") or ""
    set_code = cid.rsplit("-", 1)[0] if "-" in cid else cid
    # apitcg's per-card set.name is unreliable (it points at reprint/starter
    # sets); use the canonical name keyed by the id prefix, consistent with the
    # "Shop One Piece sets" rail.
    set_name = _OP_SET_NAMES.get(set_code, set_code)
    images = card.get("images") or {}
    img = images.get("small") or images.get("large")

    attributes: dict[str, Any] = {}
    for key in (
        "type",
        "color",
        "cost",
        "power",
        "counter",
        "family",
        "ability",
        "trigger",
    ):
        val = card.get(key)
        if val not in (None, "", [], {}):
            attributes[key] = val
    attr = card.get("attribute")
    if isinstance(attr, dict) and attr.get("name"):
        attributes["attribute"] = attr["name"]

    image_set = UnifiedImageSet(
        small=_img(img, alt=name),
        normal=_img(images.get("large") or img, alt=name),
        large=_img(images.get("large") or img, alt=name),
        art_crop=None,
    )

    set_data = UnifiedSet(
        id=f"apitcg-onepiece:{set_code}" if set_code else None,
        code=set_code or None,
        name=set_name,
        series=None,
        release_date=None,
        printed_total=None,
        total_cards=None,
        logo=None,
        symbol=None,
    )

    return UnifiedCard(
        id=f"apitcg-onepiece:{cid}",
        name=name,
        tcg="onepiece",
        set_name=set_name,
        set_code=set_code or None,
        number=card.get("code") or cid,
        rarity=card.get("rarity"),
        image_url=img,
        images=image_set,
        year=None,
        source="apitcg-onepiece",
        attributes=attributes,
        pricing_summary=None,
        set=set_data,
        tags=[],
        metadata=_meta("apitcg-onepiece"),
    ).model_dump()


# ------------------------------------------------- whole-catalog Redis cache
# apitcg (One Piece) and digimoncard.io (Digimon) have small, effectively
# static catalogs and no per-card price feed, so instead of calling the
# upstream per page/search/detail we sync the WHOLE catalog into Redis a few
# times a month and serve every read — browse, search, card detail — from that
# one cached copy. apitcg's free tier is 1000 req/mo; a full One Piece sync is
# ~32 upstream pages, so a handful of syncs a month leaves the budget almost
# untouched no matter how many users we serve. Stale-while-revalidate keeps a
# read instant even when the fresh window has lapsed, single-flight stops a
# TTL expiry from stampeding the upstream, and the budget gate blocks a
# background refresh that would breach the monthly ceiling (the long stale copy
# keeps serving until the budget resets).

_ONEPIECE_CATALOG_KEY = "loupe:public:browse:onepiece:_full"
_DIGIMON_CATALOG_KEY = "loupe:public:browse:digimon:_full"
#: Fresh a week (new sets drop ~monthly), retained two months to serve stale.
_CATALOG_FRESH_TTL = 7 * 86_400
_CATALOG_STALE_TTL = 60 * 86_400
#: A full One Piece sync is ~32 pages; only refresh if that fits the budget.
_ONEPIECE_SYNC_COST = 40


async def _onepiece_fetch() -> dict[str, Any]:
    raw = await apitcg.list_all_cards(apitcg.GAME_SLUGS["onepiece"])
    cards = [_from_apitcg_onepiece(c) for c in raw]
    cards.sort(key=lambda c: (c.get("name") or "").lower())
    return {"cards": cards}


async def _onepiece_budget_ok() -> bool:
    return await apitcg.budget.can_spend(_ONEPIECE_SYNC_COST)


async def onepiece_catalog() -> list[dict[str, Any]]:
    """Full One Piece catalog (normalized, name-sorted), served from Redis via
    stale-while-revalidate so unlimited reads cost almost no apitcg calls."""
    result = await swr_get_or_refresh(
        _ONEPIECE_CATALOG_KEY,
        fresh_ttl=_CATALOG_FRESH_TTL,
        stale_ttl=_CATALOG_STALE_TTL,
        refresh=_onepiece_fetch,
        should_refresh=_onepiece_budget_ok,
    )
    cards = result.get("cards") if isinstance(result, dict) else None
    return cards if isinstance(cards, list) else []


async def _digimon_fetch() -> dict[str, Any]:
    raw = await digimoncard.list_all()
    cards = [_from_digimon(c) for c in raw]
    cards.sort(key=lambda c: (c.get("name") or "").lower())
    return {"cards": cards}


async def digimon_catalog() -> list[dict[str, Any]]:
    """Full Digimon catalog (normalized, name-sorted), served from Redis via
    stale-while-revalidate. digimoncard.io is key-less/free, so no budget gate."""
    result = await swr_get_or_refresh(
        _DIGIMON_CATALOG_KEY,
        fresh_ttl=_CATALOG_FRESH_TTL,
        stale_ttl=_CATALOG_STALE_TTL,
        refresh=_digimon_fetch,
    )
    cards = result.get("cards") if isinstance(result, dict) else None
    return cards if isinstance(cards, list) else []


def _filter_catalog(
    cards: list[dict[str, Any]], q: str, limit: int
) -> list[dict[str, Any]]:
    """Substring-match a cached catalog by name / number / set — instant and
    free (no upstream call), and always available even if the upstream is down
    or the monthly budget is spent."""
    needle = q.strip().lower()
    if not needle:
        return cards[:limit]
    hits = [
        c
        for c in cards
        if needle in (c.get("name") or "").lower()
        or needle in str(c.get("number") or "").lower()
        or needle in (c.get("set_name") or "").lower()
    ]
    return hits[:limit]


def _find_in_catalog(
    cards: list[dict[str, Any]], card_id: str
) -> dict[str, Any] | None:
    """Locate a normalized card in a cached catalog by its unified id."""
    for c in cards:
        if c.get("id") == card_id:
            return c
    return None


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


def _build_pokemon_relaxed_query(q: str) -> str | None:
    """Forgiving fallback: OR of substring wildcards across tokens.

    The strict query ANDs prefix wildcards, so "green ninja" →
    ``name:green* name:ninja*`` matches nothing (no card has both words).
    This relaxed form — ``name:*green* OR name:*ninja*`` — surfaces partial
    and run-together matches (e.g. **Greninja**, "Green's …"), which the
    relevance scorer then ranks. Returns ``None`` when there's nothing to
    relax (single empty token)."""
    cleaned = _POKEMON_SAFE_RE.sub(" ", q).strip().lower()
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if not tokens:
        return None
    return " OR ".join(f"name:*{tok}*" for tok in tokens)


# ------------------------------------------------------------- relevance rank

#: Below this many hits a provider re-queries in a more forgiving mode.
_RELAX_MIN = 6


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance (small inputs — card names)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _ratio(a: str, b: str) -> float:
    """1.0 == identical, 0.0 == completely different."""
    if not a and not b:
        return 1.0
    m = max(len(a), len(b)) or 1
    return 1.0 - _lev(a, b) / m


def relevance_score(name: str, query: str) -> float:
    """Score a card name against the user's query (0..1, higher = better).

    Combines exact / prefix / substring / token-overlap signals with a
    de-spaced edit-distance so "green ninja" scores **Greninja** highly even
    though neither word is a prefix of it. This is what makes the search feel
    smart instead of literal."""
    n = _POKEMON_SAFE_RE.sub(" ", (name or "").lower()).strip()
    qq = _POKEMON_SAFE_RE.sub(" ", (query or "").lower()).strip()
    if not n or not qq:
        return 0.0
    if n == qq:
        return 1.0
    score = 0.0
    if n.startswith(qq) or qq.startswith(n):
        score = max(score, 0.9)
    if qq in n or n in qq:
        score = max(score, 0.85)
    qtokens = [t for t in qq.split() if t]
    ntokens = (qtokens and [t for t in n.split() if t]) or []
    if qtokens:
        exact = sum(1 for t in qtokens if t in ntokens)
        sub = sum(1 for t in qtokens if any(t in w or w in t for w in ntokens))
        score = max(score, 0.55 * exact / len(qtokens) + 0.3 * sub / len(qtokens))
    # De-spaced fuzzy match — catches run-together names + minor typos.
    score = max(score, _ratio(n.replace(" ", ""), qq.replace(" ", "")) * 0.95)
    return min(1.0, score)


def _rank(items: list[dict[str, Any]], q: str, limit: int) -> list[dict[str, Any]]:
    """Sort by relevance to ``q`` (stable), drop dupes by id, cap to ``limit``."""
    scored = sorted(
        items, key=lambda c: relevance_score(str(c.get("name", "")), q), reverse=True
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in scored:
        cid = str(c.get("id") or "")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        out.append(c)
        if len(out) >= limit:
            break
    return out


async def _search_pokemon(q: str, limit: int) -> list[dict[str, Any]]:
    raw = await pokemon_tcg.search_cards(
        _build_pokemon_query(q), page=1, page_size=limit
    )
    items = [_from_pokemon(c) for c in (raw.get("data") or [])]
    # Too few exact-ish hits → widen with the forgiving OR-substring query.
    if len(items) < _RELAX_MIN:
        relaxed = _build_pokemon_relaxed_query(q)
        if relaxed:
            try:
                raw2 = await pokemon_tcg.search_cards(
                    relaxed, page=1, page_size=max(limit, 30)
                )
                items += [_from_pokemon(c) for c in (raw2.get("data") or [])]
            except (httpx.HTTPError, CircuitOpenError, ValueError, KeyError) as exc:
                logger.info("relaxed pokemon search failed (%s): %s", q, exc)
    return _rank(items, q, limit)


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


def _build_scryfall_relaxed_query(q: str) -> str | None:
    """`name:green or name:ninja` — OR the tokens so a multi-word query that
    ANDs to nothing still surfaces partial name matches."""
    cleaned = _POKEMON_SAFE_RE.sub(" ", q).strip().lower()
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    if len(tokens) < 2:
        return None
    return " or ".join(f"name:{tok}" for tok in tokens)


async def _search_scryfall(q: str, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    err: Exception | None = None
    try:
        raw = await scryfall.search_cards(q, page=1)
        items = [_from_scryfall(c) for c in (raw.get("data") or [])]
    except (httpx.HTTPError, CircuitOpenError, ValueError, KeyError) as exc:
        # Scryfall 404s a search with zero hits — hold the error so we can
        # still attempt the relaxed query, but re-raise it below if nothing
        # is recovered (so genuine upstream outages are reported, not hidden).
        err = exc
        logger.info("scryfall search miss (%s): %s", q, exc)
    if len(items) < _RELAX_MIN:
        relaxed = _build_scryfall_relaxed_query(q)
        if relaxed:
            try:
                raw2 = await scryfall.search_cards(relaxed, page=1)
                items += [_from_scryfall(c) for c in (raw2.get("data") or [])]
                err = None  # recovered via the forgiving query
            except (httpx.HTTPError, CircuitOpenError, ValueError, KeyError) as exc:
                logger.info("relaxed scryfall search failed (%s): %s", q, exc)
    if not items and err is not None:
        raise err
    return _rank(items, q, limit)


async def _search_ygoprodeck(q: str, limit: int) -> list[dict[str, Any]]:
    raw = await ygoprodeck.search_cards(q)
    items = [_from_yugioh(c) for c in (raw.get("data") or [])]
    return _rank(items, q, limit)


async def _search_digimon(q: str, limit: int) -> list[dict[str, Any]]:
    # Served from the cached full catalog — no upstream call per search.
    items = _filter_catalog(await digimon_catalog(), q, limit * 5)
    return _rank(items, q, limit)


async def _search_onepiece(q: str, limit: int) -> list[dict[str, Any]]:
    # Served from the cached full catalog — no apitcg call per search, so it
    # stays free and works even when the monthly budget is spent.
    items = _filter_catalog(await onepiece_catalog(), q, limit * 5)
    return _rank(items, q, limit)


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
    lastgood_key = f"loupe:cards:search:lastgood:{tcg}:{q.lower()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    async def _fallback_or(body: dict[str, Any]) -> dict[str, Any]:
        """Serve the last good result when the live fetch came back empty —
        so a slow/flaky upstream never makes a known query look broken."""
        if body.get("results"):
            return body
        stale = await _cache_get(lastgood_key)
        if stale and stale.get("results"):
            return {**stale, "stale": True}
        return body

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
        elif tcg == "digimon":
            items = await _search_digimon(q, limit)
            body = {"results": items, "total": len(items), "source": "digimoncard"}
        elif tcg == "onepiece":
            items = await _search_onepiece(q, limit)
            body = {
                "results": items,
                "total": len(items),
                "source": "apitcg-onepiece",
            }
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

            # Phase 2: wait the rest of the per-provider budget for stragglers
            # if we don't yet have 2 providers — OR if Pokémon hasn't returned.
            # Pokémon is the largest catalog and the most-searched TCG, so we
            # never let fast-settle silently drop it (that's the bug where
            # "pikachu" returned nothing because Magic + Yu-Gi-Oh settled first
            # and the Pokémon task got cancelled).
            anchor_pending = not tasks["pokemontcg"].done()
            if done_count < 2 or anchor_pending:
                remaining = [t for t in tasks.values() if not t.done()]
                if remaining:
                    await asyncio.wait(
                        remaining,
                        timeout=max(0.0, PER_PROVIDER_TIMEOUT - FAST_SETTLE_DEADLINE),
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
                    logger.warning("multi-search upstream %s failed: %s", label, exc)
                    continue
                if isinstance(res, list):
                    lists.append(res)
            # Rank the combined pool by relevance to the query so the best
            # match leads regardless of provider (e.g. "green ninja" → the
            # exact Yu-Gi-Oh "Green Ninja" first, then Pokémon Greninja),
            # instead of a round-robin that buries good hits.
            merged = _rank([c for lst in lists for c in lst], q, limit)
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
        return await _fallback_or(_empty(tcg, error=str(exc)))
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("unexpected error in search_cards: %s", exc)
        return await _fallback_or(_empty(tcg, error="upstream error"))

    is_good = bool(body.get("results")) and not body.get("partial")
    if is_good:
        # A complete, non-empty result: cache it normally AND stash it as the
        # long-lived "last good" fallback for this query.
        await _cache_set(cache_key, body, CARD_SEARCH_TTL)
        await _cache_set(lastgood_key, body, LASTGOOD_RESULT_TTL)
        return body

    # Empty/partial: short-TTL it so a recovered upstream shows up on the next
    # keystroke (don't poison the 5-min cache), but serve the last good result
    # instead of an empty list when we have one — so a flaky upstream never
    # makes a known query like "mewtwo" look broken.
    await _cache_set(cache_key, body, PARTIAL_RESULT_TTL)
    return await _fallback_or(body)


# --------------------------------------------------------------------- single


async def _pricing_from_market_chain(
    name: str | None,
    set_name: str | None,
    card_id: str | None,
) -> dict[str, Any] | None:
    """Resolve a headline price from the cross-provider market chain (ordered
    fallback) for a card whose catalog upstream carried none. Consolidates every
    price API into the unified ``pricing_summary`` shape. Returns ``None`` (card
    stays unpriced) when nothing is configured or no source has a price."""
    if not name:
        return None
    from app.integrations.registry import get_registry

    # Price aggregators fuzzy-match on name; the catalog set label rarely matches
    # their set naming, so a name-led query maximizes the hit rate ("always show
    # a price"). set_name is kept for callers/precision but not forced into q.
    _ = set_name
    query = name.strip()
    try:
        mp = await get_registry().resolve_best_price(query)
    except Exception as exc:  # pragma: no cover - defensive; never block detail
        logger.debug("market-chain pricing failed for %s: %s", name, exc)
        return None
    if mp is None:
        return None
    return UnifiedPricingSummary(
        card_id=card_id,
        currency=mp.currency or "USD",
        market=_money(mp.market),
        low=_money(mp.low),
        mid=_money(mp.mid),
        high=_money(mp.high),
        as_of=None,
        sample_size=None,
        sources=[mp.source],
    ).model_dump()


# ---- catalog price enrichment (One Piece / Digimon browse tiles) -----------
# Their catalog APIs ship no prices, so we resolve a headline price from the
# cross-provider market chain, cache it per card (a day), and put it on the
# tile. Cached prices attach instantly; uncached ones resolve within a short
# wait and otherwise keep resolving in the background so the NEXT view is fully
# priced — the browse request is never blocked on a slow price provider.

price_budget = ApiBudget("pricechain", get_settings().pricechain_monthly_budget)

_PRICE_CACHE_TTL = 24 * 60 * 60  # a resolved catalog price is good for a day
_PRICE_NEG_TTL = 6 * 60 * 60  # remember "no price found" so we don't re-hammer
_ENRICH_WAIT = 1.5  # max seconds the browse request waits for fresh prices
_ENRICH_MAX_LIVE = 24  # live lookups kicked per request (≈ one page)
_price_bg_tasks: set[asyncio.Task[None]] = set()


def _price_key(card_id: str) -> str:
    return f"loupe:cardprice:{card_id}"


async def enrich_catalog_prices(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach market-chain prices to catalog-only cards, cached per card.

    Mutates and returns ``cards``. Cached prices are applied immediately; a
    bounded, budget-gated batch resolves the rest, waiting only briefly before
    returning (the stragglers keep resolving + caching for the next view)."""
    todo: list[tuple[dict[str, Any], str]] = []
    for c in cards:
        if c.get("pricing_summary"):
            continue
        key = _price_key(str(c.get("id") or ""))
        cached = await _cache_get(key)
        if isinstance(cached, dict):
            if cached:  # non-empty envelope = a real price
                c["pricing_summary"] = cached
        else:
            todo.append((c, key))

    if not todo or not await price_budget.can_spend(len(todo)):
        return cards

    async def _one(card: dict[str, Any], key: str) -> None:
        ps = await _pricing_from_market_chain(
            card.get("name"), card.get("set_name"), str(card.get("id") or "")
        )
        await price_budget.spend()
        await _cache_set(key, ps or {}, _PRICE_CACHE_TTL if ps else _PRICE_NEG_TTL)
        if ps:
            card["pricing_summary"] = ps  # mutate in place (attaches if in time)

    tasks = [asyncio.create_task(_one(c, k)) for c, k in todo[:_ENRICH_MAX_LIVE]]
    # Wait briefly for the fast ones (they attach to this response); the rest run
    # on to cache their result for the next view — never blocking the request.
    _, pending = await asyncio.wait(tasks, timeout=_ENRICH_WAIT)
    for t in pending:
        _price_bg_tasks.add(t)
        t.add_done_callback(_price_bg_tasks.discard)
    return cards


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
        elif source == "digimoncard":
            # Prefer the cached full catalog (free); fall back to a live lookup
            # only for a card not yet in the last sync.
            result = _find_in_catalog(await digimon_catalog(), card_id)
            if result is None:
                raw = await digimoncard.get_card(upstream_id)
                result = _from_digimon(raw) if raw else None
        elif source == "apitcg-onepiece":
            result = _find_in_catalog(await onepiece_catalog(), card_id)
            if result is None:
                raw = await apitcg.get_card(apitcg.GAME_SLUGS["onepiece"], upstream_id)
                result = _from_apitcg_onepiece(raw) if raw else None
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

    # Catalog-priceless games (Digimon, One Piece) carry no embedded price.
    # Fill the headline price from the cross-provider market chain so a card
    # always shows a number when ANY source has one. Only runs when the catalog
    # gave nothing, so priced games (Pokémon/Magic/Yu-Gi-Oh) pay no extra cost.
    if result is not None and not result.get("pricing_summary"):
        chain_price = await _pricing_from_market_chain(
            result.get("name"), result.get("set_name"), result.get("id")
        )
        if chain_price is not None:
            result["pricing_summary"] = chain_price

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
    if days <= 730:
        return 7
    # Multi-year "ALL" windows (e.g. a 1999 card) would otherwise mint
    # thousands of points; sample monthly so the series stays light.
    return 30


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

    # "ALL" means the card's entire lifetime — walk back to its release
    # year (Jan 1) instead of the flat 730-day fallback, so a 1999 card
    # shows ~25y of history rather than stopping ~2 years ago. We cap the
    # lookback so a bad/missing year can't request a 1000-year window.
    if range_ == "all":
        card_year = card.get("year")
        if isinstance(card_year, int) and 1900 < card_year <= utcnow().year:
            lifetime_days = (utcnow().date() - date(card_year, 1, 1)).days
            days = max(_RANGE_DAYS["all"], min(lifetime_days, 60 * 365))

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
    seed_key = hashlib.sha256(f"{card_id}|{house_key}|{grade_key}".encode()).hexdigest()
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
# Graded-price multiplier vs the raw price, per numeric grade. Covers the full
# 1-10 scale incl. half grades so every tier the UI offers (PSA/BGS/CGC/SGC)
# charts a sensible curve — gem-mint commands a steep premium, low grades trade
# below raw. (Replaced by real PriceCharting comps once a token is configured.)
_GRADE_MULT_HISTORY: dict[float, tuple[float, float]] = {
    10: (10.0, 18.0),
    9.5: (5.0, 8.0),
    9: (2.5, 4.0),
    8.5: (1.6, 2.2),
    8: (1.2, 1.6),
    7.5: (1.0, 1.3),
    7: (0.9, 1.1),
    6.5: (0.80, 0.92),
    6: (0.70, 0.78),
    5.5: (0.62, 0.70),
    5: (0.55, 0.62),
    4.5: (0.50, 0.55),
    4: (0.45, 0.52),
    3.5: (0.40, 0.45),
    3: (0.35, 0.40),
    2.5: (0.30, 0.35),
    2: (0.25, 0.30),
    1.5: (0.20, 0.25),
    1: (0.15, 0.20),
}


def _parse_grade(grade_key: str) -> float | None:
    """Accept ``"10"``, ``"9.5"``, ``"PSA 10"``, ``"10 BLACK"`` → float."""
    if not grade_key:
        return None
    s = grade_key.strip().lower()
    # Strip a leading house token like "psa 10".
    for h in ("psa", "cgc", "bgs", "sgc", "tag"):
        if s.startswith(h):
            s = s[len(h) :].strip()
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


def _digimon_sets_from_catalog(
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive a set list from the flat Digimon catalog (no sets endpoint).

    Groups by the printed-id set prefix (``BT17-017`` → ``BT17``), counting
    cards and keeping the human set name. Ordered biggest-set-first so the main
    booster sets lead the "Shop sets" rail.
    """
    groups: dict[str, dict[str, Any]] = {}
    for c in cards:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        code = cid.rsplit("-", 1)[0] if "-" in cid else cid
        sn = c.get("set_name")
        if isinstance(sn, list):
            sn = sn[0] if sn else None
        g = groups.setdefault(code, {"name": sn, "count": 0})
        g["count"] += 1
        if not g["name"] and sn:
            g["name"] = sn
    out = [
        {
            "id": f"digimoncard:{code}",
            "code": code,
            "name": g["name"] or code,
            "tcg": "digimon",
            "release_date": None,
            "total_cards": g["count"],
            "image_url": None,
            "source": "digimoncard",
        }
        for code, g in groups.items()
    ]
    out.sort(key=lambda s: (-(s["total_cards"] or 0), s["name"] or ""))
    return out


# One Piece booster-set names. apitcg's per-card set.name is unreliable (it
# points at reprint/starter sets) and its /sets list only has 2, so we key off
# the canonical id prefix (OP03-070 → OP03). These English names are stable;
# any unknown code (future sets, ST decks, promos) falls back to the code, which
# collectors recognize.
_OP_SET_NAMES: dict[str, str] = {
    "OP01": "Romance Dawn",
    "OP02": "Paramount War",
    "OP03": "Pillars of Strength",
    "OP04": "Kingdoms of Intrigue",
    "OP05": "Awakening of the New Era",
    "OP06": "Wings of the Captain",
    "OP07": "500 Years in the Future",
    "OP08": "Two Legends",
    "OP09": "Emperors in the New World",
    "OP10": "Royal Blood",
    "OP11": "A Fist of Divine Speed",
    "EB01": "Memorial Collection",
    "EB02": "Anime 25th Collection",
    "PRB01": "ONE PIECE THE BEST",
    "P": "Promotional Cards",
}


def _onepiece_sets_from_catalog(
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive One Piece sets from the flat catalog, grouped by id prefix."""
    counts: dict[str, int] = {}
    for c in cards:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        code = cid.rsplit("-", 1)[0] if "-" in cid else cid
        counts[code] = counts.get(code, 0) + 1
    out = [
        {
            "id": f"apitcg-onepiece:{code}",
            "code": code,
            "name": _OP_SET_NAMES.get(code, code),
            "tcg": "onepiece",
            "release_date": None,
            "total_cards": n,
            "image_url": None,
            "source": "apitcg-onepiece",
        }
        for code, n in counts.items()
    ]

    # Booster sets (OP*) first by recency, then everything else by size.
    def _op_set_sort(s: dict[str, Any]) -> tuple[int, int, int]:
        code = str(s.get("code") or "")
        num = int(code[2:]) if code[2:].isdigit() else 0
        total = int(s.get("total_cards") or 0)
        return (0 if code.startswith("OP") else 1, -num, -total)

    out.sort(key=_op_set_sort)
    return out


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
        elif tcg == "digimon":
            cards = await digimoncard.list_all()
            items = _digimon_sets_from_catalog(cards)
            body = {"results": items, "total": len(items), "source": "digimoncard"}
        elif tcg == "onepiece":
            cards = await apitcg.list_all_cards(apitcg.GAME_SLUGS["onepiece"])
            items = _onepiece_sets_from_catalog(cards)
            body = {
                "results": items,
                "total": len(items),
                "source": "apitcg-onepiece",
            }
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
