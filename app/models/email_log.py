"""EmailLog — one row per outbound email, from queue to delivery.

The send pipeline writes ``queued → sent | failed`` (with the rendered
content, so a failed send can be retried and a support agent can see exactly
what a user received); the Resend webhook advances rows to
``delivered | bounced | complained`` by ``provider_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol

#: Lifecycle values for :attr:`EmailLog.status`.
EMAIL_STATUSES = ("queued", "sent", "failed", "delivered", "bounced", "complained")


class EmailLog(Base):
    __tablename__ = "email_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    to_email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    # Best-effort link to the recipient account (SET NULL if they're deleted).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidCol(),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    category: Mapped[str | None] = mapped_column(String(40), index=True, nullable=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    # The exact rendered message — powers the admin preview and true retries.
    html: Mapped[str | None] = mapped_column(Text(), nullable=True)
    text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Resend message id — how webhook events find their row.
    provider_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="queued", index=True, nullable=False
    )
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["EMAIL_STATUSES", "EmailLog"]
