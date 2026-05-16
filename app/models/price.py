"""PriceSnapshot ORM model — observed sale/price for a card+grade combo."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol
from app.models.enums import GradeHouseEnum, PriceSourceEnum


class PriceSnapshot(Base):
    """A single recorded price observation."""

    __tablename__ = "price_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("cards.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    house: Mapped[GradeHouseEnum] = mapped_column(
        Enum(GradeHouseEnum, name="grade_house_enum"), nullable=False
    )
    grade: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    source: Mapped[PriceSourceEnum] = mapped_column(
        Enum(PriceSourceEnum, name="price_source_enum"), nullable=False
    )
    price_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    sale_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_price_snapshots_card_grade_date", "card_id", "grade", "sale_date"),
    )


__all__ = ["PriceSnapshot"]
