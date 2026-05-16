"""Wire schemas for ``GET /v1/cards/{id}/listings``."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.market import Money


class ListingWire(BaseModel):
    source: str
    title: str
    price: Money
    url: str = ""
    condition: str | None = None
    image_url: str | None = None
    is_auction: bool = False
    time_left_seconds: int | None = None


class ListingsResponse(BaseModel):
    card_id: str
    query: str
    listings: list[ListingWire]


__all__ = ["ListingWire", "ListingsResponse"]
