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
    #: The composite catalog id (`<source>:<external_id>`) the card resolves to,
    #: e.g. `pokemontcg:base1-4` — so a client that only knows the upstream id
    #: (browse/card-detail) can match its "already has an alert?" state.
    upstream_id: str | None = None
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
    # Identify the card by whatever id the client has: a local catalog UUID *or*
    # a composite upstream id (`pokemontcg:base1-4`) — the browse/card-detail
    # views only know the latter. The backend resolves + materializes it, so
    # `card_id` accepts either form (the legacy `upstream_id` field still works).
    card_id: str | uuid.UUID | None = None
    upstream_id: str | None = Field(None, max_length=128)
    condition: PriceAlertCondition
    threshold_usd: Decimal = Field(..., gt=Decimal("0"))
    note: str | None = Field(None, max_length=280)

    @model_validator(mode="after")
    def _require_a_card(self) -> Self:
        if not self.card_id and not self.upstream_id:
            raise ValueError("Provide either card_id or upstream_id.")
        return self

    @property
    def card_ref(self) -> str:
        """The single card reference to resolve (UUID or composite)."""
        return str(self.card_id) if self.card_id else (self.upstream_id or "")


__all__ = ["PriceAlertCreate", "PriceAlertRead"]
