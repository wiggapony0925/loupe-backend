"""GradedCard ORM model — outcome of a grading pipeline run."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol
from app.models.enums import GradeHouseEnum


class GradedCard(Base):
    """A graded card belonging to a user."""

    __tablename__ = "graded_cards"

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
        ForeignKey("cards.id", ondelete="RESTRICT"),
        index=True,
        nullable=False,
    )
    scan_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("scan_jobs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    grade: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    house: Mapped[GradeHouseEnum] = mapped_column(
        Enum(GradeHouseEnum, name="grade_house_enum"),
        default=GradeHouseEnum.loupe,
        nullable=False,
    )
    subgrades: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    estimated_value_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    # ── Cost basis (user-supplied) ─────────────────────────────────────────
    # What the user paid for the card. Drives true P/L on the portfolio
    # chart: `unrealizedPnlUsd = estimated_value_usd - purchase_price_usd`.
    # Both columns are nullable so legacy / scanned-only rows stay valid;
    # the UI treats `null` as "no cost recorded" rather than zero.
    purchase_price_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    purchase_date: Mapped[date | None] = mapped_column(
        Date(), nullable=True
    )
    fingerprint_hash: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    graded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_graded_cards_user_graded_at", "user_id", "graded_at"),)


__all__ = ["GradedCard"]
