"""PriceAlert ORM model — user-defined trigger on a card's market price.

When the daily price-backfill worker writes a new price, an alert
evaluator (see `app/workers/price_alert_worker.py`) compares the latest
price against any active alert thresholds and sets `triggered_at` when
the condition is first met. Each alert fires at most once; users
re-arm by deleting + recreating.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol
from app.models.enums import PriceAlertCondition


class PriceAlert(Base):
    """A user's price trigger for one card."""

    __tablename__ = "price_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("cards.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    condition: Mapped[PriceAlertCondition] = mapped_column(
        Enum(PriceAlertCondition, name="price_alert_condition"),
        nullable=False,
    )
    threshold_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(280), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Snapshot of the price that fired the alert — useful for the
    # notification body and the "Triggered at $X" badge in the UI.
    triggered_price_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    __table_args__ = (
        Index("ix_price_alerts_user_card", "user_id", "card_id"),
        Index("ix_price_alerts_active", "card_id", "triggered_at"),
    )


__all__ = ["PriceAlert"]
