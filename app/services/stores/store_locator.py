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

import json
import math
import re
from typing import Any

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.schemas.stores import NearbyStore, NearbyStoresRead
from app.utils.logger import get_logger

logger = get_logger("services.stores")

#: Tried in order — the main instance 504s under load; Kumi is a large
#: community mirror. Both are free.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_TIMEOUT_S = 25.0
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


def _overpass_query(lat: float, lng: float, radius_m: int) -> str:
    shops = "|".join(sorted(CORE_SHOP_TAGS | LIKELY_SHOP_TAGS))
    # Two selectors: the shop-tag net, plus a name net that catches card
    # shops OSM has tagged as something else entirely.
    # The name net spans shop/craft/amenity/office rather than shop alone —
    # card shops are mapped under all four. Each selector stays TAG-ANCHORED
    # on purpose: an unanchored ["name"~…] regex makes Overpass scan every
    # named feature in the radius, which times out.
    name_re = "card|tcg|pok[eé]mon|yu.?gi|collectib"
    return f"""
[out:json][timeout:{int(OVERPASS_TIMEOUT_S)}];
(
  nwr["shop"~"^({shops})$"](around:{radius_m},{lat},{lng});
  nwr["shop"]["name"~"{name_re}",i](around:{radius_m},{lat},{lng});
  nwr["craft"]["name"~"{name_re}",i](around:{radius_m},{lat},{lng});
);
out center {MAX_RESULTS * 3};
""".strip()


def _category_for(shop_tag: str, name: str) -> str | None:
    """The label a client renders under the pin — backend-owned wording."""
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


async def _fetch_overpass(
    lat: float, lng: float, radius_m: int
) -> list[dict[str, Any]]:
    query = _overpass_query(lat, lng, radius_m)
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        timeout=OVERPASS_TIMEOUT_S + 5, headers={"User-Agent": USER_AGENT}
    ) as client:
        for url in OVERPASS_URLS:
            try:
                resp = await client.post(url, data={"data": query})
                resp.raise_for_status()
                return resp.json().get("elements", [])
            except Exception as exc:  # try the next mirror
                last_error = exc
                logger.warning("Overpass endpoint %s failed: %s", url, exc)
    raise last_error if last_error else RuntimeError("no overpass endpoints")


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
