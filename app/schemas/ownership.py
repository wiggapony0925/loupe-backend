"""Per-user ownership context for a card — composed onto the card view.

`GET /v1/cards/{id}/ownership` returns this for the signed-in user: every copy
they own (each a `GradedCard`), with grade/condition/scan data and per-holding +
rolled-up cost basis, holding value, and unrealized P/L.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import AcquisitionSourceEnum, GradeHouseEnum, RawConditionEnum


class CardHolding(BaseModel):
    """One owned copy of a card (a `GradedCard`), with derived figures."""

    model_config = ConfigDict(extra="forbid")

    holding_id: uuid.UUID
    grade: Decimal
    house: GradeHouseEnum
    is_graded: bool
    condition: RawConditionEnum | None = None
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = None
    purchase_price_usd: Decimal | None = None
    purchase_date: date | None = None
    acquired_via: AcquisitionSourceEnum | None = None
    scan_job_id: uuid.UUID | None = None
    fingerprint_hash: str | None = None
    notes: str | None = None
    graded_at: datetime
    # ── Derived ──
    days_held: int | None = None
    unrealized_pl_usd: Decimal | None = None
    unrealized_pl_pct: float | None = None


class CardOwnership(BaseModel):
    """Rolled-up ownership of one card for the signed-in user."""

    model_config = ConfigDict(extra="forbid")

    owned: bool = False
    copies: int = 0
    holdings: list[CardHolding] = []
    # ── Rolled-up across holdings ──
    cost_basis_usd: Decimal | None = None
    holding_value_usd: Decimal | None = None
    unrealized_pl_usd: Decimal | None = None
    unrealized_pl_pct: float | None = None


__all__ = ["CardHolding", "CardOwnership"]
