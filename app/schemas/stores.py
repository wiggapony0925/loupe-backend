"""Schemas for the card-shop locator (``/v1/public/stores``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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


class NearbyStoresRead(BaseModel):
    """Ranked nearby shops (dedicated card stores first, then distance)."""

    stores: list[NearbyStore] = []
    #: ``live`` | ``cached`` | ``unavailable`` (upstream down → empty list).
    source: Literal["live", "cached", "unavailable"] = "live"


__all__ = ["NearbyStore", "NearbyStoresRead"]
