"""Pydantic schemas for the PriceAlert resource."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Identify the card by *either* a local catalog UUID (mobile, which has
    # already resolved a local card) *or* an upstream ``<source>:<id>`` id
    # (web card-detail, which only knows the composite id). The service
    # materializes a local Card from ``upstream_id`` when ``card_id`` is absent.
    card_id: uuid.UUID | None = None
    upstream_id: str | None = Field(None, max_length=128)
    condition: PriceAlertCondition
    threshold_usd: Decimal = Field(..., gt=Decimal("0"))
    note: str | None = Field(None, max_length=280)

    @model_validator(mode="after")
    def _require_a_card(self) -> Self:
        if self.card_id is None and not self.upstream_id:
            raise ValueError("Provide either card_id or upstream_id.")
        return self


__all__ = ["PriceAlertCreate", "PriceAlertRead"]
