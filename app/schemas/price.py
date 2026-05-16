"""PriceSnapshot schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import GradeHouseEnum, PriceSourceEnum


class PriceSnapshotRead(BaseModel):
    """Public representation of a single price observation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    card_id: uuid.UUID
    house: GradeHouseEnum
    grade: Decimal
    source: PriceSourceEnum
    price_usd: Decimal
    sale_date: date | None = None
    created_at: datetime


class PriceQuery(BaseModel):
    """Query params for ``GET /v1/prices``."""

    card_id: uuid.UUID
    house: GradeHouseEnum | None = None
    grade: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("10"))
    source: PriceSourceEnum | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=100)


__all__ = ["PriceQuery", "PriceSnapshotRead"]
