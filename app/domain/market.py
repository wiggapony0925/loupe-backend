"""Market-domain value objects shared across integrations and services.

These are the canonical shapes that vendor adapters in
``app.integrations.*`` produce and that services in ``app.services.*``
consume. They are intentionally plain ``dataclass`` instances (no
Pydantic, no ORM, no FastAPI) so they remain cheap to construct and
trivially picklable for cache/queue use.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Listing:
    """Live for-sale listing."""

    source: str
    title: str
    price: float
    currency: str = "USD"
    url: str = ""
    condition: str | None = None
    image_url: str | None = None
    is_auction: bool = False
    time_left_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SoldComp:
    """One completed sale."""

    source: str
    title: str
    price: float
    sold_at: str  # ISO 8601 UTC, ends with ``Z``
    currency: str = "USD"
    condition: str | None = None
    grade: str | None = None
    house: str | None = None
    url: str | None = None
    image_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PopulationReport:
    """Population for a single (house, grade) tier."""

    source: str
    house: str
    grade: str
    population: int
    pop_higher: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketPrice:
    """Snapshot price for a product (low/mid/high/market in USD)."""

    source: str
    market: float | None = None
    low: float | None = None
    mid: float | None = None
    high: float | None = None
    currency: str = "USD"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
