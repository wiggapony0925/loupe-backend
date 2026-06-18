"""Wire schemas for ``GET /v1/cards/{id}/nearby-listings``.

Facebook Marketplace listings near the user's device location, powering the
"Near You" carousel on the card-detail sheet. Mirrors the shape produced by
:mod:`app.services.market.nearby_listings_service`.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.market import Money


class GeoPoint(BaseModel):
    """A latitude/longitude pair (the search centre)."""

    lat: float
    lng: float


class NearbyListingWire(BaseModel):
    """One Facebook Marketplace listing with optional geo metadata."""

    source: str = "facebook"
    title: str
    price: Money
    url: str = ""
    condition: str | None = None
    image_url: str | None = None
    distance_km: float | None = None
    location_label: str | None = None


class NearbyListingsResponse(BaseModel):
    card_id: str
    query: str
    center: GeoPoint
    radius_km: int
    listings: list[NearbyListingWire]


__all__ = [
    "GeoPoint",
    "NearbyListingWire",
    "NearbyListingsResponse",
]
