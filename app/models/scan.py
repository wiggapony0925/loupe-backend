"""ScanJob ORM model — represents a 4-angle card capture & grading run."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import JsonCol, UuidCol
from app.models.enums import ScanSourceEnum, ScanStatusEnum


class ScanJob(Base):
    """One scan request comprising up to 4 angle images and a grading result."""

    __tablename__ = "scan_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    scanner_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("scanners.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    status: Mapped[ScanStatusEnum] = mapped_column(
        Enum(ScanStatusEnum, name="scan_status_enum"),
        default=ScanStatusEnum.queued,
        nullable=False,
        index=True,
    )
    source: Mapped[ScanSourceEnum] = mapped_column(
        Enum(ScanSourceEnum, name="scan_source_enum"),
        default=ScanSourceEnum.phone,
        nullable=False,
    )
    images_s3_keys: Mapped[dict | None] = mapped_column(JsonCol, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_scan_jobs_user_status_created", "user_id", "status", "created_at"),
    )


__all__ = ["ScanJob"]
