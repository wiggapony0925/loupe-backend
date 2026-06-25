"""Schemas for the admin card explorer + manual price override."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import GradeHouseEnum


class AdminCardRow(BaseModel):
    """A card row in the admin explorer search results."""

    id: uuid.UUID
    name: str
    set_name: str | None = None
    number: str | None = None
    tcg: str
    rarity: str | None = None
    year: int | None = None
    image_url: str | None = None


class AdminCardPage(BaseModel):
    results: list[AdminCardRow]
    total: int
    page: int
    page_size: int


class ExternalRefRead(BaseModel):
    source: str
    external_id: str
    confidence: float | None = None


class PriceSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    house: str
    grade: float
    source: str
    price_usd: float
    sale_date: date | None = None
    created_at: datetime


class AdminCardDetail(AdminCardRow):
    """Full card record — catalog fields, provider refs, and the price ladder."""

    set_id: uuid.UUID
    image_phash: str | None = None
    card_metadata: dict | None = None
    external_refs: list[ExternalRefRead]
    prices: list[PriceSnapshotRead]


class PriceOverrideRequest(BaseModel):
    """Manually record a price point (stored as a `manual`-source snapshot)."""

    house: GradeHouseEnum = GradeHouseEnum.loupe
    grade: float = Field(ge=0, le=10)
    price_usd: float = Field(gt=0)
    sale_date: date | None = None


__all__ = [
    "AdminCardDetail",
    "AdminCardPage",
    "AdminCardRow",
    "ExternalRefRead",
    "PriceOverrideRequest",
    "PriceSnapshotRead",
]
