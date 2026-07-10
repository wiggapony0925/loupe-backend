"""UserReport ORM model — generated portfolio statements (monthly / yearly).

Each row represents one rendered PDF statement attached to a user. The
PDF itself is stored in object storage (GCS / S3) and referenced here
by `storage_key`; the row is the source of truth for "this user has a
statement for May 2026". Rows persist for the lifetime of the user's
account so they can re-download at any time.

`status` follows a strict lifecycle: `pending` → (`ready` | `failed`).
Only `ready` rows have a populated `storage_key` + `file_size_bytes`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol
from app.models.enums import ReportPeriodEnum, ReportStatusEnum


class UserReport(Base):
    """A generated portfolio statement (PDF) attached to a user."""

    __tablename__ = "user_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    period: Mapped[ReportPeriodEnum] = mapped_column(
        Enum(ReportPeriodEnum, name="report_period_enum"),
        nullable=False,
    )
    # Optional collection scope. NULL = whole-vault statement. Not an FK —
    # a deleted collection must never cascade away the user's historical
    # PDFs; `collection_name` is baked at generation time so the archive
    # row stays labelled even after the collection is gone.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(UuidCol(), nullable=True)
    collection_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Inclusive period bounds in UTC. For "May 2026" that's
    # (2026-05-01, 2026-05-31); for the year 2026 that's
    # (2026-01-01, 2026-12-31). The tuple is unique per (user, period,
    # collection scope) so a given month/year can only be generated once
    # per scope.
    period_start: Mapped[date] = mapped_column(Date(), nullable=False)
    period_end: Mapped[date] = mapped_column(Date(), nullable=False)
    status: Mapped[ReportStatusEnum] = mapped_column(
        Enum(ReportStatusEnum, name="report_status_enum"),
        default=ReportStatusEnum.pending,
        nullable=False,
    )
    # Storage backend key (e.g. "reports/<user_id>/<id>.pdf"). Populated
    # once status flips to `ready`; null while pending or failed.
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Short human-readable label baked at generation time. Lets the API
    # render "May 2026 statement" without re-deriving from period dates
    # on every list call.
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    __table_args__ = (
        # One report per (user, period type, period start, collection scope).
        # Re-running generation for the same window updates the existing row
        # in place rather than racing two PDFs. NULL collection_id (whole
        # vault) dedupe is additionally enforced app-side in
        # `_get_or_create_pending` because Postgres treats NULLs as distinct.
        UniqueConstraint(
            "user_id",
            "period",
            "period_start",
            "collection_id",
            name="uq_user_reports_user_period_start_scope",
        ),
        Index("ix_user_reports_user_created_at", "user_id", "created_at"),
    )


__all__ = ["UserReport"]
