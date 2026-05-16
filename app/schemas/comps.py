"""Wire schemas for ``GET /v1/cards/{id}/comps``."""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.market import Money


class SoldCompWire(BaseModel):
    source: str
    title: str
    price: Money
    sold_at: str
    condition: str | None = None
    grade: str | None = None
    house: str | None = None
    url: str | None = None
    image_url: str | None = None


class CompsFilters(BaseModel):
    grade: str | None = None
    house: str | None = None


class CompsResponse(BaseModel):
    card_id: str
    query: str
    days: int
    filters: CompsFilters
    comps: list[SoldCompWire]


__all__ = ["CompsFilters", "CompsResponse", "SoldCompWire"]
