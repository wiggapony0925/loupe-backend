"""Local mirror of the PriceCharting price guide (Legendary CSV bulk download).

Empty until a Legendary subscription's daily CSV is synced (see
``app.integrations.pricecharting.csv_sync``). When populated, per-card price
lookups resolve from here — instant, unlimited, and quota-free — instead of the
1-request/second live API. Zero effect on any other tier: if the table is empty
the provider simply falls back to the API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol


class PriceChartingPrice(Base):
    """One PriceCharting product row (a card / sealed SKU) with its ladder."""

    __tablename__ = "pricecharting_prices"

    #: PriceCharting's unique product id.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    product_name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Lowercased name for indexed matching against a free-text card query.
    product_name_lower: Mapped[str] = mapped_column(
        String(300), nullable=False, index=True
    )
    console_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    console_lower: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    #: Best-effort tcg mapping from the console name ("pokemon" / "magic" / …).
    tcg: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    #: Raw / ungraded price (USD) — the quick-sort column.
    loose_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Full grade ladder ``{grade label → USD}`` (same shape everything else uses).
    ladder: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    sales_volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    release_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["PriceChartingPrice"]
