"""GradedCard ORM model — outcome of a grading pipeline run."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
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
from app.db.types import JsonCol, UuidCol
from app.models.enums import AcquisitionSourceEnum, GradeHouseEnum, RawConditionEnum


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
    # PSA-style condition for RAW (ungraded) cards. Only meaningful when
    # `house == loupe` (our placeholder for "raw / not slabbed"). Nullable
    # so legacy rows + slabbed cards stay valid; UI hides the chip when
    # the card has a third-party grade since the slab already says it.
    condition: Mapped[RawConditionEnum | None] = mapped_column(
        Enum(RawConditionEnum, name="raw_condition_enum"),
        nullable=True,
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
    purchase_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    fingerprint_hash: Mapped[str | None] = mapped_column(
        String(128), index=True, nullable=True
    )
    # How the card entered the collection (scan / manual / import). Nullable so
    # legacy rows stay valid; the UI reads it as "unknown" when absent.
    acquired_via: Mapped[AcquisitionSourceEnum | None] = mapped_column(
        Enum(AcquisitionSourceEnum, name="acquisition_source_enum"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # User-defined organization tags for this holding (e.g. "PC", "For sale",
    # "Trade"). A small JSON array of short strings — per-holding so different
    # copies of the same card can be tagged differently. Nullable so legacy rows
    # stay valid; readers treat null as an empty list.
    tags: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
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

    __table_args__ = (
        Index("ix_graded_cards_user_graded_at", "user_id", "graded_at"),
        # The three vault filter/sort indexes. Migration 0040 created them
        # directly in postgres and no model ever declared them, so they existed
        # only in databases that had been migrated — a create_all-built one
        # (the tests, a fresh local bootstrap) sorted and filtered the vault by
        # sequential scan. Declared here so the models are the whole truth.
        Index("ix_graded_cards_user_active", "user_id", "deleted_at"),
        Index("ix_graded_cards_user_value", "user_id", "estimated_value_usd"),
        Index("ix_graded_cards_user_grade", "user_id", "grade"),
        # 0 through 10, matching the API contract (`GradedCardCreate.grade` is
        # ge=0/le=10) rather than any one house's scale. The bounds are chosen
        # from what the code supports, not from PSA's rulebook:
        #
        #   * The top is 10. Every house we recognise (PSA/BGS/CGC/SGC/TAG)
        #     stops at 10, and `_GRADE_MULT_HISTORY` in card_search_service —
        #     the ladder that turns a grade into a price — is only defined
        #     from 1 to 10 in half steps.
        #   * The bottom is 0, not 1, because 0 is a real value here: it is
        #     the sentinel graded_card_service writes for RAW holdings
        #     (`house == loupe`), where `condition` carries the meaning
        #     instead. Refusing it would refuse every ungraded card.
        #
        # Worth enforcing in the database rather than only in pydantic because
        # grade drives the price lookup and therefore a user's reported
        # portfolio total: one 99.9 from a broken import or a hand-run backfill
        # silently inflates a number the user trusts, and no read path
        # re-validates it. Half steps are deliberately NOT enforced — the
        # column is Numeric(4, 1) and a house that starts issuing 9.2 should
        # not need a migration.
        CheckConstraint("grade >= 0 AND grade <= 10", name="ck_graded_card_grade"),
    )

    @property
    def is_graded(self) -> bool:
        """True when slabbed by a third-party house; ``loupe`` is our raw/ungraded
        placeholder, so it reads as not graded."""
        return self.house != GradeHouseEnum.loupe


__all__ = ["GradedCard"]
