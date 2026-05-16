"""GradedCard schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import GradeHouseEnum


class GradedCardRead(BaseModel):
    """Public representation of a graded card."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    card_id: uuid.UUID
    scan_job_id: uuid.UUID | None = None
    grade: Decimal
    house: GradeHouseEnum
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = None
    fingerprint_hash: str | None = None
    notes: str | None = None
    graded_at: datetime
    created_at: datetime
    updated_at: datetime
    # Joined card metadata so clients can render a row without an N+1 round-trip.
    card_name: str | None = None
    card_image_url: str | None = None
    card_number: str | None = None
    card_set_name: str | None = None
    card_year: int | None = None
    card_tcg: str | None = None


class GradedCardCreate(BaseModel):
    """Body for manually creating a graded card (admin / import flows)."""

    card_id: uuid.UUID
    grade: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10"))
    house: GradeHouseEnum = GradeHouseEnum.loupe
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = Field(None, ge=Decimal("0"))
    notes: str | None = Field(None, max_length=2000)
    scan_job_id: uuid.UUID | None = None
    fingerprint_hash: str | None = Field(None, max_length=128)


class GradedCardUpdate(BaseModel):
    """Body for ``PATCH /v1/grades/{id}``."""

    notes: str | None = Field(None, max_length=2000)
    estimated_value_usd: Decimal | None = Field(None, ge=Decimal("0"))


__all__ = ["GradedCardCreate", "GradedCardRead", "GradedCardUpdate"]
