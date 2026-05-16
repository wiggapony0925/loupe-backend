"""Market snapshot schemas.

Rich per-house × per-grade market view returned by
``GET /v1/cards/{id}/market`` (see :mod:`app.services.market_service`).

Money/pricing field shapes mirror the dict shape the upstream search
service already produces — kept as a thin BaseModel so downstream
clients have a typed contract to lean on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Money(BaseModel):
    """Currency-tagged amount."""

    model_config = ConfigDict(from_attributes=True)

    amount: float
    currency: str = "USD"


class PricePoint(BaseModel):
    """Single point on a price-history walk."""

    ts: str
    price: float
    currency: str = "USD"
    source: str = "synthetic"


class PriceHistorySummary(BaseModel):
    """Aggregate stats over a :class:`PriceHistory` window."""

    min: float | None = None
    max: float | None = None
    avg: float | None = None
    current: float | None = None
    change_pct: float | None = None
    n_points: int = 0


class PriceHistory(BaseModel):
    """Time series for one range bucket."""

    card_id: str
    currency: str = "USD"
    points: list[PricePoint] = Field(default_factory=list)
    granularity: Literal["daily", "weekly", "monthly"] = "daily"
    range: str
    summary: PriceHistorySummary


class MarketSummary(BaseModel):
    """Headline numbers shown above the per-house breakdown."""

    raw: Money | None = None
    graded_avg: Money | None = None
    pop_top: Money | None = None
    pop_total: int = 0
    change_pct_1y: float = 0.0
    last_sale_at: datetime | None = None
    primary_house: str = "psa"


class HouseGradeRow(BaseModel):
    """One row in the house × grade table."""

    house: str
    grade: float
    grade_label: str
    population: int
    market: Money
    change_pct: float
    last_sale_at: datetime | None = None
    listing_url: str | None = None


class HouseBlock(BaseModel):
    """All graded rows for a single house, sorted high → low grade."""

    house: str
    pop_total: int
    grades: list[HouseGradeRow] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    """Composed market view for a single card."""

    summary: MarketSummary
    history: dict[str, PriceHistory] = Field(default_factory=dict)
    houses: list[HouseBlock] = Field(default_factory=list)
    tiers_total: int = 0


class MarketResponse(BaseModel):
    """Top-level body wrapped by the envelope middleware."""

    card_id: str
    snapshot: MarketSnapshot


__all__ = [
    "HouseBlock",
    "HouseGradeRow",
    "MarketResponse",
    "MarketSnapshot",
    "MarketSummary",
    "Money",
    "PriceHistory",
    "PriceHistorySummary",
    "PricePoint",
]
