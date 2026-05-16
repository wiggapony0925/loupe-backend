"""User and UserSettings ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UuidCol


class User(Base):
    """An end-user authenticated via Apple or Google sign-in."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    apple_subject: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    google_subject: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    # argon2id hash; NULL for SSO-only accounts that never set a password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    settings: Mapped[UserSettings | None] = relationship(
        "UserSettings",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class UserSettings(Base):
    """Per-user app preferences (theme, currency, sync flags)."""

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    theme: Mapped[str] = mapped_column(String(16), default="system", nullable=False)
    live_sync_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    push_notifications_enabled: Mapped[bool] = mapped_column(
        default=True, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship("User", back_populates="settings")


__all__ = ["User", "UserSettings"]
