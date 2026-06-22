"""Apify provider — Facebook Marketplace nearby listings.

Powers the "Near You" carousel on the card-detail sheet. Given a card
search query plus the user's device location, this fetches real Facebook
Marketplace listings within a radius via an Apify actor's synchronous
run endpoint.

Unlike the other providers, the nearby search needs geo arguments
(latitude / longitude / radius), so it exposes a dedicated
``search_nearby_listings`` method that callers invoke directly rather than
through the capability-based fan-out in the registry. This keeps the shared
``BaseProvider`` contract unchanged while still reusing the common HTTP
client, retry helper, and error-swallowing conventions.

Every method swallows upstream errors and returns ``[]`` so the card-detail
endpoint never surfaces a 5xx because Facebook/Apify hiccuped.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, Listing
from app.utils.logger import get_logger

logger = get_logger("integrations.apify")

_API_BASE = "https://api.apify.com/v2"

#: Apify actors can be slow (they spin up a browser). The card-detail
#: fan-out budget is tight, but this provider is called on its own
#: (location-gated, user-initiated) path, so we allow a longer ceiling.
_RUN_TIMEOUT_S = 30.0


class ApifyProvider(BaseProvider):
    """Facebook Marketplace listings near a coordinate, via Apify."""

    id = "apify_fb"
    name = "Facebook Marketplace"

    def is_configured(self) -> bool:
        return bool(get_settings().apify_api_token)

    async def search_nearby_listings(
        self,
        query: str,
        *,
        lat: float,
        lng: float,
        radius_km: int = 40,
        limit: int = 20,
    ) -> list[tuple[Listing, dict[str, Any]]]:
        """Return ``(Listing, geo_extras)`` pairs near ``(lat, lng)``.

        ``geo_extras`` carries fields that don't fit the shared ``Listing``
        dataclass (``distance_km``, ``location_label``) so the service layer
        can surface them without widening the domain object.

        Returns ``[]`` on any failure or when not configured.
        """
        settings = get_settings()
        token = settings.apify_api_token
        if not token or not query:
            return []

        # Apify's API path uses `username~actor-name`, not `username/actor-name`.
        actor = settings.apify_fb_marketplace_actor.replace("/", "~")
        url = f"{_API_BASE}/acts/{actor}/run-sync-get-dataset-items?token={token}"
        # apify/facebook-marketplace-scraper takes `startUrls` (a real FB
        # Marketplace search URL), not query+lat/lng. Location is best-effort:
        # FB honours latitude/longitude/radius on the search URL when present,
        # otherwise it falls back to a general search.
        fb_url = (
            "https://www.facebook.com/marketplace/search/"
            f"?query={quote(query)}"
            f"&latitude={lat}&longitude={lng}&radius={radius_km}"
        )
        payload = {
            "startUrls": [{"url": fb_url}],
            "resultsLimit": limit,
            "includeListingDetails": False,
        }

        try:
            resp = await self._call_with_retry(
                "POST",
                url,
                json=payload,
                timeout=_RUN_TIMEOUT_S,
                retries=1,
            )
            if resp is None or resp.status_code >= 400:
                if resp is not None:
                    logger.info(
                        "apify run non-2xx: %s %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                return []
            items = resp.json()
        except Exception as exc:  # pragma: no cover - network/parse guard
            logger.debug("apify nearby fetch failed: %s", exc)
            return []

        if not isinstance(items, list):
            return []

        out: list[tuple[Listing, dict[str, Any]]] = []
        for raw in items:
            mapped = _map_item(raw)
            if mapped is not None:
                out.append(mapped)
            if len(out) >= limit:
                break
        return out


def _map_item(raw: Any) -> tuple[Listing, dict[str, Any]] | None:
    """Map one Apify dataset item → ``(Listing, geo_extras)``.

    Apify actors vary in their output shape, so we read defensively across
    the common key spellings and bail (return ``None``) if there's no usable
    price + title.
    """
    if not isinstance(raw, dict):
        return None

    title = _first_str(
        raw, "marketplace_listing_title", "custom_title", "title", "name"
    )
    if not title:
        return None

    # apify/facebook-marketplace-scraper nests price/photo/location; read those
    # first, then fall back to flat spellings other actors might use.
    price = _nested_number(raw, "listing_price", "amount") or _first_number(
        raw, "price", "amount", "priceAmount"
    )
    # Per the contract above, a listing with no usable price is unusable data.
    if price is None:
        return None
    image_url = _nested_str(
        raw, "primary_listing_photo", "photo_image_url"
    ) or _first_str(raw, "image", "imageUrl", "primaryImage", "thumbnail")
    url = _first_str(raw, "listingUrl", "url", "link", "permalink") or ""
    condition = _first_str(raw, "condition", "itemCondition")
    currency = _first_str(raw, "currency", "priceCurrency") or "USD"
    distance_km = _first_number(raw, "distanceKm", "distance_km", "distance")
    location_label = _fb_location(raw) or _first_str(
        raw, "locationLabel", "city", "locationText"
    )

    listing = Listing(
        source="facebook",
        title=str(title),
        price=float(price) if price is not None else 0.0,
        currency=str(currency).upper()[:3] or "USD",
        url=str(url),
        condition=condition,
        image_url=image_url,
        is_auction=False,
        time_left_seconds=None,
    )
    geo_extras: dict[str, Any] = {
        "distance_km": distance_km,
        "location_label": location_label,
    }
    return listing, geo_extras


def _first_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _first_number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        parsed = _to_number(value)
        if parsed is not None:
            return parsed
    return None


def _nested_str(data: dict[str, Any], outer: str, inner: str) -> str | None:
    obj = data.get(outer)
    if isinstance(obj, dict):
        value = obj.get(inner)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _nested_number(data: dict[str, Any], outer: str, inner: str) -> float | None:
    obj = data.get(outer)
    return _to_number(obj.get(inner)) if isinstance(obj, dict) else None


def _fb_location(data: dict[str, Any]) -> str | None:
    """Human label from FB's nested ``location.reverse_geocode`` block."""
    loc = data.get("location")
    geo = loc.get("reverse_geocode") if isinstance(loc, dict) else None
    if not isinstance(geo, dict):
        return None
    page = geo.get("city_page")
    if isinstance(page, dict):
        name = page.get("display_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    parts = [p for p in (geo.get("city"), geo.get("state")) if isinstance(p, str) and p]
    return ", ".join(parts) or None


def _to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


__all__ = ["ApifyProvider"]
