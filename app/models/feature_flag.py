"""FeatureFlag ORM model — runtime on/off switches for features.

Flags let admins hide or reveal pages, components, and whole "micro-apps"
on web *and* mobile without a deploy: clients read the public flag map and
gate UI on it. Identified by a stable string ``key`` (e.g. ``web_markets``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UuidCol


class FeatureFlag(Base):
    """A single named feature toggle."""

    __tablename__ = "feature_flags"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    key: Mapped[str] = mapped_column(
        String(80), unique=True, index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["FeatureFlag"]
