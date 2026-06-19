"""Careers/hiring ORM models — job postings, applications, and the event
trail used to keep applicants informed.

These power the public careers page and the admin developer portal. Status
fields are stored as plain strings (the enum *values* from
:mod:`app.models.enums`) so the schema stays portable across SQLite and
Postgres without native enum-type migrations; validation lives in the
Pydantic layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UuidCol
from app.models.enums import (
    ApplicationStatusEnum,
    EmploymentTypeEnum,
    JobStatusEnum,
)


class JobPosting(Base):
    """A single open (or draft/closed) role."""

    __tablename__ = "job_postings"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(
        String(160), unique=True, index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    team: Mapped[str] = mapped_column(String(80), nullable=False)
    location: Mapped[str] = mapped_column(String(120), nullable=False)
    employment_type: Mapped[str] = mapped_column(
        String(20), default=EmploymentTypeEnum.full_time.value, nullable=False
    )
    summary: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str] = mapped_column(Text(), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=JobStatusEnum.draft.value, index=True, nullable=False
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

    applications: Mapped[list[JobApplication]] = relationship(
        "JobApplication",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (Index("ix_job_postings_status_created", "status", "created_at"),)


class JobApplication(Base):
    """Someone's application to a :class:`JobPosting`.

    Applicants are not necessarily Loupe users, so identity is captured by
    email rather than a FK to ``users``.
    """

    __tablename__ = "job_applications"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("job_postings.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    applicant_name: Mapped[str] = mapped_column(String(160), nullable=False)
    applicant_email: Mapped[str] = mapped_column(
        String(320), index=True, nullable=False
    )
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resume_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cover_letter: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        default=ApplicationStatusEnum.submitted.value,
        index=True,
        nullable=False,
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

    job: Mapped[JobPosting] = relationship("JobPosting", back_populates="applications")
    events: Mapped[list[ApplicationEvent]] = relationship(
        "ApplicationEvent",
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ApplicationEvent.created_at",
    )

    __table_args__ = (Index("ix_job_applications_job_created", "job_id", "created_at"),)


class ApplicationEvent(Base):
    """A status change / note on an application — the applicant-facing trail.

    Created on submission and on every admin status update. ``message`` is
    the optional note shown to the applicant; ``notified`` records whether
    an outbound notification was dispatched for this event.
    """

    __tablename__ = "application_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    notified: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    application: Mapped[JobApplication] = relationship(
        "JobApplication", back_populates="events"
    )


__all__ = ["ApplicationEvent", "JobApplication", "JobPosting"]
