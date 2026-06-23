"""Nearby-listings service — card + user location → Facebook Marketplace.

Resolves the viewed card into a search query, then asks the Apify provider
for Facebook Marketplace listings within ``radius_km`` of the user's device
location. Powers the "Near You" carousel on the card-detail sheet.

Caching: keyed by card + *rounded* coordinates (≈1 km grid) so we cache per
neighbourhood rather than per GPS jitter, and so raw device coordinates never
become part of a long-lived cache key. The raw lat/lng are sent only to our
backend over HTTPS and forwarded to Apify; they are never logged or persisted.
"""

from __future__ import annotations

import json
from typing import Any

from app.integrations.apify import ApifyProvider
from app.platform.redis_client import get_redis
from app.services.catalog import card_search_service
from app.utils.logger import get_logger

logger = get_logger("services.nearby_listings")

_CACHE_PREFIX = "loupe:cards:nearby"
_CACHE_TTL = 300  # seconds — FB listings churn, but 5 min keeps it snappy.

# Round coordinates to 2 dp (~1.1 km) for the cache key.
_COORD_PRECISION = 2

# When nothing sells within the asked radius, widen the search to ~national
# scope so we can still surface the *closest* listings (FB returns results
# ordered by distance from the point). The client shows the per-listing
# distance, so the user always sees how far the nearest copy actually is.
_FALLBACK_RADIUS_KM = 800


def _build_query(card: dict[str, Any]) -> str:
    parts: list[str] = []
    name = card.get("name")
    if name:
        parts.append(str(name))
    set_name = card.get("set_name") or (card.get("set") or {}).get("name")
    if set_name:
        parts.append(str(set_name))
    number = card.get("number")
    if number:
        parts.append(f"#{number}")
    return " ".join(parts).strip()


async def _cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # pragma: no cover
        logger.debug("nearby cache_get failed: %s", exc)
        return None


async def _cache_set(key: str, value: Any) -> None:
    try:
        r = await get_redis()
        await r.setex(key, _CACHE_TTL, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        logger.debug("nearby cache_set failed: %s", exc)


async def get_nearby_listings_for_card(
    card_id: str,
    *,
    lat: float,
    lng: float,
    radius_km: int = 40,
    limit: int = 20,
) -> dict[str, Any] | None:
    """Return Facebook Marketplace listings near ``(lat, lng)`` for a card.

    Returns ``None`` when the card cannot be resolved (→ 404). Returns a body
    with ``listings: []`` when Apify is unconfigured or has no results, so the
    client always renders a clean empty state.
    """
    rlat = round(lat, _COORD_PRECISION)
    rlng = round(lng, _COORD_PRECISION)
    cache_key = f"{_CACHE_PREFIX}:{card_id}:{rlat}:{rlng}:{radius_km}:{limit}"

    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    card = await card_search_service.get_card(card_id)
    if card is None:
        return None

    query = _build_query(card)
    provider = ApifyProvider()
    pairs = await provider.search_nearby_listings(
        query, lat=lat, lng=lng, radius_km=radius_km, limit=limit
    )

    # Nothing in the asked radius → widen so we still return the closest copies.
    expanded = False
    if not pairs and radius_km < _FALLBACK_RADIUS_KM:
        expanded = True
        pairs = await provider.search_nearby_listings(
            query, lat=lat, lng=lng, radius_km=_FALLBACK_RADIUS_KM, limit=limit
        )

    listings = [
        {
            "source": listing.source,
            "title": listing.title,
            "price": {"amount": listing.price, "currency": listing.currency},
            "url": listing.url,
            "condition": listing.condition,
            "image_url": listing.image_url,
            "distance_km": geo.get("distance_km"),
            "location_label": geo.get("location_label"),
        }
        for listing, geo in pairs
    ]
    # Closest first when distance is known; keep provider order otherwise.
    listings.sort(
        key=lambda x: (
            x["distance_km"] is None,
            x["distance_km"] if x["distance_km"] is not None else 0.0,
        )
    )

    body = {
        "card_id": card_id,
        "query": query,
        "center": {"lat": rlat, "lng": rlng},
        "radius_km": _FALLBACK_RADIUS_KM if expanded else radius_km,
        # True when we widened past the requested radius to find the closest.
        "expanded": expanded,
        "listings": listings,
    }
    await _cache_set(cache_key, body)
    return body


__all__ = ["get_nearby_listings_for_card"]
