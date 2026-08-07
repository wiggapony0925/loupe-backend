"""Card-shop locator — "stores near me that sell trading cards".

Data source is the OpenStreetMap **Overpass API** (free, no key, no per-
request billing): we query shop nodes/ways in a radius around the caller
and keep the ones that plausibly sell trading cards — dedicated game and
collectible stores always, hobby/toy/comic shops as likely carriers, and
ANY shop whose name mentions cards/TCG/Pokémon and friends.

Cost control (the whole point of doing this server-side):
  • Requests are snapped to a ~1.1 km grid + radius bucket, and each grid
    cell is cached in the durable L2 (`kv_cache`) for 24 h — map pans and
    repeat opens hit the cache, Overpass sees a given neighborhood at
    most once a day.
  • The upstream query is bounded (timeout + result cap) and the endpoint
    is rate-limited at the router.

The backend owns ranking (likelihood + distance) and the category label a
client shows — clients render the list verbatim, per the house rule.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.schemas.stores import NearbyStore, NearbyStoresRead
from app.utils.logger import get_logger

logger = get_logger("services.stores")

#: Tried in order, HEDGED (see _run_query) — all free, all public, and all
#: verified to carry PLANET data, not a regional extract. That check is not
#: optional: overpass.osm.ch looks perfect (fast, HTTP 200) but only holds
#: Switzerland, so it answered "0 shops in Times Square" in under a second
#: and won the hedge race with an empty result. Order is by measured health
#: from the US; the last-good endpoint is remembered and tried first.
OVERPASS_URLS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
#: Head start a mirror gets before the next one is raced alongside it.
HEDGE_DELAY_S = 4.0
#: Mirror that last answered — sticky, so a healthy one keeps being used.
_preferred_url: str | None = None
OVERPASS_TIMEOUT_S = 25.0
#: Extra seconds the expensive name query gets AFTER the tag query answers.
#: Past this the user gets the tag results rather than a longer spinner.
NAME_GRACE_S = 8.0
#: OSM fair-use etiquette: identify the app with a contactable User-Agent.
USER_AGENT = "Loupe/1.0 (card-shop locator; https://loupe.app)"
CACHE_TTL_S = 24 * 3600
MAX_RESULTS = 90

#: Shop tags that ARE the target (a card/game/collectible store)…
CORE_SHOP_TAGS = {"games", "collector", "trading_cards", "video_games"}
#: …and tags that often carry cards (toy stores, comic shops, hobby shops).
#: Kept deliberately tight. Adding high-cardinality tags (department_store,
#: variety_store, antiques…) made the Overpass query 504 in dense cities —
#: measured. Shops of those kinds still surface via the NAME net below.
LIKELY_SHOP_TAGS = {"toys", "hobby", "comics", "anime", "books", "stationery"}

#: WORD-BOUNDED name matching — substring checks classed "Cardullo's
#: Gourmet Shoppe" and "Riccardi" (a boutique) as card stores. A strong
#: match promotes any shop to a card store; a likely match keeps it as a
#: probable carrier.
STRONG_NAME_RE = re.compile(r"\bcards?\b|\btcg\b|trading.?cards?", re.IGNORECASE)
#: A shop whose NAME is about games/hobby sells cards often enough to list.
GAME_NAME_RE = re.compile(
    r"\bgames?\b|\bgaming\b|game\s?stop|games?\s?workshop|\bhobb(y|ies)\b",
    re.IGNORECASE,
)
#: Names that match the card regexes but are certainly not card shops — a
#: laundromat called "Card-op", greeting-card and credit-card businesses.
#: Checked FIRST, so a strong "card" match can't promote them.
EXCLUDE_NAME_RE = re.compile(
    r"laundr|dry.?clean|credit.?card|debit.?card|key.?card|card.?board|"
    r"greeting.?cards?|hallmark|card.?access",
    re.IGNORECASE,
)
LIKELY_NAME_RE = re.compile(
    r"pok[eé]mon|yu.?gi.?oh|collectib|\bcomics?\b|\bgames?\b|\bhobby\b",
    re.IGNORECASE,
)


def _store_key(store_id: str) -> str:
    """Per-store row so store DETAIL doesn't need the grid it came from."""
    return f"stores:one:v1:{store_id}"


def _grid_key(lat: float, lng: float, radius_km: float) -> str:
    """Snap the query to a ~1.1 km grid so nearby requests share a cache row."""
    return f"stores:nearby:v1:{round(lat, 2)}:{round(lng, 2)}:{int(radius_km)}"


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine — good enough for "how far is the shop"."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _tag_query(lat: float, lng: float, radius_m: int) -> str:
    """Indexed shop-tag lookup — cheap, works even in dense cities."""
    shops = "|".join(sorted(CORE_SHOP_TAGS | LIKELY_SHOP_TAGS))
    return f"""
[out:json][timeout:{int(OVERPASS_TIMEOUT_S)}];
nwr["shop"~"^({shops})$"](around:{radius_m},{lat},{lng});
out center {MAX_RESULTS * 3};
""".strip()


def _name_query(lat: float, lng: float, radius_m: int) -> str:
    """Name lookup — catches card shops tagged as something else, but makes
    Overpass scan every shop name in the radius, so it is the expensive half
    and is allowed to fail on its own without taking the tag net with it."""
    name_re = "card|tcg|pok[eé]mon|yu.?gi|collectib"
    # ONE tag-anchored selector. Adding a second ["craft"] selector alongside
    # this took the same query from 4 s to 40 s+ (it timed out outright in
    # the NYC metro) and contributed no shops — card stores are not mapped
    # as crafts. Measured, not guessed.
    return f"""
[out:json][timeout:{int(OVERPASS_TIMEOUT_S)}];
nwr["shop"]["name"~"{name_re}",i](around:{radius_m},{lat},{lng});
out center {MAX_RESULTS * 2};
""".strip()


def _category_for(shop_tag: str, name: str) -> str | None:
    """The label a client renders under the pin — backend-owned wording."""
    # A core shop TAG is authoritative; a name match is not, so names that
    # are known false friends are dropped before any promotion happens.
    if shop_tag not in CORE_SHOP_TAGS and EXCLUDE_NAME_RE.search(name):
        return None
    if shop_tag in CORE_SHOP_TAGS or STRONG_NAME_RE.search(name):
        return "Card & game store"
    # "Games Workshop", "GameStop", "… Hobby" — a game/hobby NAME is a
    # strong enough signal even when the tag is generic or missing.
    if GAME_NAME_RE.search(name):
        return "Card & game store"
    if shop_tag == "comics":
        return "Comic shop"
    if shop_tag == "toys":
        return "Toy store"
    if shop_tag in ("hobby", "anime"):
        return "Hobby shop"
    if LIKELY_NAME_RE.search(name):
        return "May carry cards"
    return None  # not plausibly a card carrier → dropped


def _address_from(tags: dict[str, Any]) -> str | None:
    parts = [
        " ".join(
            x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x
        ),
        tags.get("addr:city"),
    ]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _parse_elements(
    elements: list[dict[str, Any]], lat: float, lng: float
) -> list[NearbyStore]:
    seen: set[str] = set()
    out: list[NearbyStore] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        # Ways/relations carry their centroid under "center".
        el_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        el_lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if el_lat is None or el_lng is None:
            continue
        category = _category_for(tags.get("shop", ""), name)
        if category is None:
            continue
        # A shop mapped as both node and way appears twice — dedupe on
        # name + rounded position.
        dedupe = f"{name.lower()}:{round(el_lat, 4)}:{round(el_lng, 4)}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append(
            NearbyStore(
                id=f"osm:{el.get('type', 'node')}:{el.get('id')}",
                name=name,
                lat=float(el_lat),
                lng=float(el_lng),
                distance_km=round(_distance_km(lat, lng, el_lat, el_lng), 2),
                category=category,
                address=_address_from(tags),
                website=tags.get("website") or tags.get("contact:website"),
                phone=tags.get("phone") or tags.get("contact:phone"),
                opening_hours=tags.get("opening_hours"),
                photo_url=tags.get("image"),
                wikidata_id=tags.get("brand:wikidata") or tags.get("wikidata"),
            )
        )
    # Dedicated card/game stores first, then by distance.
    out.sort(key=lambda s: (s.category != "Card & game store", s.distance_km))
    return out[:MAX_RESULTS]


def _ordered_endpoints() -> list[str]:
    """Mirrors to try, last known-good first."""
    if _preferred_url and _preferred_url in OVERPASS_URLS:
        return [_preferred_url, *(u for u in OVERPASS_URLS if u != _preferred_url)]
    return list(OVERPASS_URLS)


async def _post_query(client: httpx.AsyncClient, url: str, query: str) -> list[Any]:
    resp = await client.post(url, data={"data": query})
    resp.raise_for_status()
    elements = resp.json().get("elements", [])
    return list(elements)


async def _run_query(query: str) -> list[dict[str, Any]]:
    """One Overpass query, HEDGED across the mirrors. Raises if all fail.

    Mirrors were tried strictly one after another, so a sick endpoint cost
    its full timeout before we even tried a healthy one — measured at 19 s
    for a query that a healthy mirror answers in 4 s, and 64 s when two in
    a row were down. That was the bulk of "it takes forever to find stores".

    Now the first mirror gets a head start; if it hasn't answered within
    ``HEDGE_DELAY_S`` the next one joins in parallel, and the first success
    wins while the stragglers are cancelled. A dead mirror costs 4 s, not
    25 s, and fanning out gradually keeps us polite to a free service.
    """
    global _preferred_url
    errors: list[Exception] = []
    # An empty 200 is a PROVISIONAL answer, never an instant win: a mirror
    # serving a regional extract answers "nothing here" faster than a
    # healthy mirror answers correctly. Held until everything else settles.
    empty_win: list[Any] | None = None
    async with httpx.AsyncClient(
        timeout=OVERPASS_TIMEOUT_S + 5, headers={"User-Agent": USER_AGENT}
    ) as client:
        running: dict[asyncio.Task[list[Any]], str] = {}
        try:
            for url in _ordered_endpoints():
                task = asyncio.create_task(_post_query(client, url, query))
                running[task] = url
                # Give the leader a head start before hedging onto the next.
                done, _ = await asyncio.wait(
                    running, timeout=HEDGE_DELAY_S, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    src = running.pop(finished)
                    try:
                        result = finished.result()
                    except Exception as exc:
                        errors.append(exc)
                        logger.warning("Overpass endpoint %s failed: %s", src, exc)
                        continue
                    if not result:
                        empty_win = result
                        continue
                    _preferred_url = src  # stick to what just worked
                    return result

            # Every mirror is in flight — wait it out.
            while running:
                done, _ = await asyncio.wait(
                    running, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    src = running.pop(finished)
                    try:
                        result = finished.result()
                    except Exception as exc:
                        errors.append(exc)
                        logger.warning("Overpass endpoint %s failed: %s", src, exc)
                        continue
                    if not result:
                        empty_win = result
                        continue
                    _preferred_url = src
                    return result
        finally:
            for task in running:
                task.cancel()

    if empty_win is not None:
        return empty_win  # genuinely nothing here — all mirrors agree
    raise errors[0] if errors else RuntimeError("no overpass endpoints")


async def _fetch_overpass(
    lat: float, lng: float, radius_m: int
) -> list[dict[str, Any]]:
    """Tag net + name net, run CONCURRENTLY with a soft deadline on the slow one.

    These used to run one after the other: the cheap indexed tag query, THEN
    the expensive name scan — up to ~45 s before the app saw a single shop.
    They now start together, and once the tag results are in the name query
    only gets a short grace period before we answer with what we have. The
    grid row it would have enriched gets refreshed on the next search of the
    area, so nothing is lost permanently.
    """
    tag_task = asyncio.create_task(_run_query(_tag_query(lat, lng, radius_m)))
    name_task = asyncio.create_task(_run_query(_name_query(lat, lng, radius_m)))

    results: list[dict[str, Any]] = []
    errors: list[Exception] = []

    try:
        results.extend(await tag_task)
    except Exception as exc:
        errors.append(exc)
        logger.warning("overpass tag query failed: %s", exc)

    # The name net is a bonus, not the answer. Give it a grace window, then
    # stop waiting — a user staring at a spinner is the worse failure.
    try:
        results.extend(await asyncio.wait_for(name_task, timeout=NAME_GRACE_S))
    except TimeoutError:
        name_task.cancel()
        logger.info(
            "name query exceeded %ss grace — answering with tag results", NAME_GRACE_S
        )
    except Exception as exc:
        errors.append(exc)
        logger.warning("overpass name query failed: %s", exc)

    if not results and errors:
        raise errors[0]
    return results


async def nearby_stores(
    lat: float, lng: float, radius_km: float = 25.0
) -> NearbyStoresRead:
    """Card shops around a point — cached per grid cell for a day."""
    key = _grid_key(lat, lng, radius_km)
    cached = await kv_get(key)
    if cached:
        try:
            doc = json.loads(cached)
            stores = [NearbyStore(**s) for s in doc["stores"]]
            if not stores:
                # A poisoned empty row from before the guard above — ignore
                # it and re-search rather than serving "no shops" again.
                raise ValueError("empty cached row")
            # Index on the cached path TOO: areas searched before per-store
            # rows existed would otherwise 404 on detail until their grid
            # row expired.
            await _index_stores(stores)
            return NearbyStoresRead(stores=stores, source="cached")
        except Exception:
            logger.warning("stores cache row unreadable; refetching")

    try:
        elements = await _fetch_overpass(lat, lng, int(radius_km * 1000))
    except Exception as exc:
        logger.warning("Overpass fetch failed (%s); serving empty result", exc)
        return NearbyStoresRead(stores=[], source="unavailable")

    stores = _parse_elements(elements, lat, lng)
    if not stores:
        # NEVER cache an empty area for a day. Overpass answers 200 with an
        # empty element list when its own server-side timeout trips, so a
        # single slow moment would otherwise lock a real neighbourhood to
        # "no shops" for 24 h — which is exactly what happened to Queens.
        logger.warning(
            "empty store result at %.3f,%.3f (r=%skm) — not caching",
            lat,
            lng,
            radius_km,
        )
        return NearbyStoresRead(stores=[], source="live")
    await kv_set(
        key,
        json.dumps({"stores": [s.model_dump() for s in stores]}),
        ttl_seconds=CACHE_TTL_S,
    )
    await _index_stores(stores)
    return NearbyStoresRead(stores=stores, source="live")


async def _index_stores(stores: list[NearbyStore]) -> None:
    """Index each store on its own key so DETAIL can resolve one by id
    without knowing which search found it."""
    for store in stores:
        await kv_set(
            _store_key(store.id), store.model_dump_json(), ttl_seconds=CACHE_TTL_S
        )


async def store_by_id(store_id: str) -> NearbyStore | None:
    """A single cached store, or ``None`` if we've never seen it."""
    raw = await kv_get(_store_key(store_id))
    if not raw:
        return None
    try:
        return NearbyStore.model_validate_json(raw)
    except Exception:  # poisoned row → treat as a miss
        logger.warning("store cache row unreadable for %s", store_id)
        return None


__all__ = ["nearby_stores", "store_by_id"]
