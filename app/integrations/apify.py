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

        actor = settings.apify_fb_marketplace_actor
        url = (
            f"{_API_BASE}/acts/{actor}/run-sync-get-dataset-items"
            f"?token={token}"
        )
        payload = {
            "query": query,
            "latitude": lat,
            "longitude": lng,
            "radiusKm": radius_km,
            "maxItems": limit,
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

    title = _first_str(raw, "title", "name", "marketplace_listing_title")
    price = _first_number(raw, "price", "amount", "listing_price", "priceAmount")
    if not title or price is None:
        return None

    url = _first_str(raw, "url", "listingUrl", "link", "permalink") or ""
    image_url = _first_str(
        raw,
        "image",
        "imageUrl",
        "primaryImage",
        "thumbnail",
        "primary_listing_photo",
    )
    condition = _first_str(raw, "condition", "itemCondition")
    currency = _first_str(raw, "currency", "priceCurrency") or "USD"
    distance_km = _first_number(raw, "distanceKm", "distance_km", "distance")
    location_label = _first_str(
        raw,
        "location",
        "locationLabel",
        "city",
        "locationText",
    )

    listing = Listing(
        source="facebook",
        title=str(title),
        price=float(price),
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
