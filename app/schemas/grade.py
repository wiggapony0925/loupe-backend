"""GradedCard schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    # Cost basis (user-supplied). `null` means "no cost recorded" —
    # the UI hides P/L for that row rather than treating it as \$0.
    purchase_price_usd: Decimal | None = None
    purchase_date: date | None = None
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
    """Body for manually creating a graded card (admin / import flows).

    Either ``card_id`` (an existing local UUID) or ``upstream_id`` (a
    composite ``<source>:<external_id>`` from a catalog hit / scan /
    deep link) must be supplied. When ``upstream_id`` is used, the
    server materializes the local :class:`Card` + :class:`CardExternalRef`
    via :func:`card_resolver_service.ensure_local_card` before inserting
    the grade — so the user never has to think about ids.
    """

    card_id: uuid.UUID | None = None
    upstream_id: str | None = Field(
        None,
        max_length=240,
        description="Composite catalog id like 'pokemontcg:base1-4'.",
    )
    grade: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10"))
    house: GradeHouseEnum = GradeHouseEnum.loupe
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = Field(None, ge=Decimal("0"))
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"))
    purchase_date: date | None = None
    notes: str | None = Field(None, max_length=2000)
    scan_job_id: uuid.UUID | None = None
    fingerprint_hash: str | None = Field(None, max_length=128)


class GradedCardUpdate(BaseModel):
    """Body for ``PATCH /v1/grades/{id}``."""

    notes: str | None = Field(None, max_length=2000)
    estimated_value_usd: Decimal | None = Field(None, ge=Decimal("0"))
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"))
    purchase_date: date | None = None


__all__ = ["GradedCardCreate", "GradedCardRead", "GradedCardUpdate"]
