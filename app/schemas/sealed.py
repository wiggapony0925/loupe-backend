"""Pydantic schemas for sealed-product catalog + ownership endpoints.

Validators here mirror :mod:`app.schemas.grade` so the two collection
surfaces stay symmetric — anything we tightened for graded cards
(future-date guard, control-char strip, MSRP/value ceiling) we
re-apply for sealed.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SealedProductTypeEnum, TcgEnum

_MAX_VALUE_USD = Decimal("10000000")
_MIN_RELEASE_YEAR = 1990  # First Pokemon Base Set is 1996; leave headroom for vintage
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CTRL_RE.sub("", value).strip()
    return cleaned or None


# ── Catalog ───────────────────────────────────────────────────────────────


class SealedProductRead(BaseModel):
    """Public catalog row returned by ``GET /v1/sealed/search``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tcg: TcgEnum
    product_type: SealedProductTypeEnum
    set_id: uuid.UUID | None = None
    name: str
    set_name: str | None = None
    image_url: str | None = None
    msrp_usd: Decimal | None = None
    release_date: date | None = None


class SealedPricePoint(BaseModel):
    """One point on a sealed product's value line."""

    ts: str  # ISO date
    price: float


class SealedMarketRead(BaseModel):
    """Live market snapshot for one sealed product (``GET /v1/sealed/{id}/market``).

    Sealed SKUs have no stored daily history, so the snapshot is a current
    TCGplayer quote (low/mid/high/market) + MSRP. ``points`` is a real value
    line anchored at MSRP-on-release → current market, so the UI can chart how
    the product appreciated since launch (no fabricated points).
    """

    product_id: uuid.UUID
    currency: str = "USD"
    msrp_usd: Decimal | None = None
    market: float | None = None
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    source: str | None = None
    marketplace_url: str | None = None
    points: list[SealedPricePoint] = []


# ── Holdings ──────────────────────────────────────────────────────────────


class SealedHoldingRead(BaseModel):
    """A user's sealed holding plus joined product metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    purchase_price_usd: Decimal | None = None
    purchase_date: date | None = None
    estimated_value_usd: Decimal | None = None
    notes: str | None = None
    opened_at: datetime | None = None
    acquired_at: datetime
    created_at: datetime
    updated_at: datetime
    # Joined catalog metadata so the vault can render a row without an
    # N+1 fetch. Optional because tests may construct holdings stand-alone.
    product_name: str | None = None
    product_image_url: str | None = None
    product_type: SealedProductTypeEnum | None = None
    product_tcg: TcgEnum | None = None
    product_set_name: str | None = None


class SealedHoldingCreate(BaseModel):
    """Body for ``POST /v1/sealed-holdings``."""

    product_id: uuid.UUID
    quantity: int = Field(1, ge=1, le=10_000)
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"), le=_MAX_VALUE_USD)
    purchase_date: date | None = None
    estimated_value_usd: Decimal | None = Field(
        None, ge=Decimal("0"), le=_MAX_VALUE_USD
    )
    notes: str | None = Field(None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, v: str | None) -> str | None:
        return _strip_control_chars(v)

    @field_validator("purchase_date")
    @classmethod
    def _validate_purchase_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = date.today()
        if v > today:
            raise ValueError("purchase_date cannot be in the future")
        if v.year < _MIN_RELEASE_YEAR:
            raise ValueError(f"purchase_date year must be >= {_MIN_RELEASE_YEAR}")
        return v


class SealedHoldingUpdate(BaseModel):
    """Body for ``PATCH /v1/sealed-holdings/{id}``.

    All fields optional. ``opened_at`` is a one-way toggle on the
    client (set it to the current time when the user "rips" the box),
    but we accept any timestamp here so import flows can backfill.
    """

    quantity: int | None = Field(None, ge=1, le=10_000)
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"), le=_MAX_VALUE_USD)
    purchase_date: date | None = None
    estimated_value_usd: Decimal | None = Field(
        None, ge=Decimal("0"), le=_MAX_VALUE_USD
    )
    notes: str | None = Field(None, max_length=2000)
    opened_at: datetime | None = None

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, v: str | None) -> str | None:
        return _strip_control_chars(v)

    @field_validator("purchase_date")
    @classmethod
    def _validate_purchase_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = date.today()
        if v > today:
            raise ValueError("purchase_date cannot be in the future")
        if v.year < _MIN_RELEASE_YEAR:
            raise ValueError(f"purchase_date year must be >= {_MIN_RELEASE_YEAR}")
        return v


__all__ = [
    "SealedHoldingCreate",
    "SealedHoldingRead",
    "SealedHoldingUpdate",
    "SealedProductRead",
]
