"""Loupe Scanner waitlist ORM model.

Backs the public "Join the waitlist" checkout flow on the scanner product
page and the admin Waitlist pipeline. Like job applications, a signup is
keyed by **email** (not a FK to ``users``) because anyone can join, but we
*link* the ``user_id`` opportunistically when the visitor is authenticated
so the portal can show which real accounts are in line.

Status is stored as the string value of
:class:`~app.models.enums.WaitlistStatusEnum` to keep the schema portable
across SQLite and Postgres (validation lives in the Pydantic layer).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol
from app.models.enums import WaitlistStatusEnum


class WaitlistEntry(Base):
    """One person waiting to buy a Loupe Scanner."""

    __tablename__ = "waitlist_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # What they collect / why they want a scanner — free text from the
    # checkout form. Optional; helps prioritise outreach.
    interest: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # How they found us (referral, social, etc.) — optional analytics signal.
    referral_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Linked Loupe account when the visitor was signed in. Set NULL on delete
    # so removing a user doesn't drop their place in line / our record.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # Quantity they intend to buy (the checkout stepper). Defaults to 1.
    quantity: Mapped[int] = mapped_column(default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        default=WaitlistStatusEnum.waiting.value,
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

    __table_args__ = (
        Index("ix_waitlist_entries_status_created", "status", "created_at"),
    )


__all__ = ["WaitlistEntry"]
