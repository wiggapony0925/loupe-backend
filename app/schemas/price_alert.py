"""Pydantic schemas for the PriceAlert resource."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PriceAlertCondition


class PriceAlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    card_id: uuid.UUID
    condition: PriceAlertCondition
    threshold_usd: Decimal
    note: str | None = None
    created_at: datetime
    triggered_at: datetime | None = None
    triggered_price_usd: Decimal | None = None
    # Joined card metadata so vault / card-detail rows render without an
    # N+1 fetch.
    card_name: str | None = None
    card_image_url: str | None = None


class PriceAlertCreate(BaseModel):
    card_id: uuid.UUID
    condition: PriceAlertCondition
    threshold_usd: Decimal = Field(..., gt=Decimal("0"))
    note: str | None = Field(None, max_length=280)


__all__ = ["PriceAlertCreate", "PriceAlertRead"]
