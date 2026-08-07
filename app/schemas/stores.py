"""Schemas for the card-shop locator (``/v1/public/stores``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class NearbyStore(BaseModel):
    """One physical shop near the caller. Positions are WGS84."""

    #: Stable upstream id (``osm:<type>:<id>``) — dedupe / feedback key.
    id: str
    name: str
    lat: float
    lng: float
    distance_km: float
    #: Backend-owned label the client renders verbatim ("Card & game store").
    category: str
    address: str | None = None
    website: str | None = None
    phone: str | None = None
    opening_hours: str | None = None
    #: Photo the shop publishes (OSM ``image`` tag, else its site's
    #: og:image). ``None`` = the client renders its own art block.
    photo_url: str | None = None
    #: Community rating over Loupe reviews (``None`` until someone rates).
    rating: float | None = None
    review_count: int = 0


class NearbyStoresRead(BaseModel):
    """Ranked nearby shops (dedicated card stores first, then distance)."""

    stores: list[NearbyStore] = []
    #: ``live`` | ``cached`` | ``unavailable`` (upstream down → empty list).
    source: Literal["live", "cached", "unavailable"] = "live"


class StoreReviewRead(BaseModel):
    """One collector's review of a shop."""

    id: uuid.UUID
    store_id: str
    rating: int
    body: str | None = None
    created_at: datetime
    #: Author identity (social profile), so clients render a real person.
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    #: True when the caller wrote it (edit/delete affordances).
    is_mine: bool = False


class StoreReviewUpsert(BaseModel):
    """Body for ``PUT /v1/social/stores/{store_id}/review``."""

    rating: int = Field(..., ge=1, le=5)
    body: str | None = Field(None, max_length=1000)


class StoreDetailRead(BaseModel):
    """``GET /v1/public/stores/{store_id}`` — one shop, fully dressed."""

    store: NearbyStore
    reviews: list[StoreReviewRead] = []


__all__ = [
    "NearbyStore",
    "NearbyStoresRead",
    "StoreDetailRead",
    "StoreReviewRead",
    "StoreReviewUpsert",
]
