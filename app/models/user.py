"""User and UserSettings ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
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
    # DB-backed admin grant. Effective admin = `is_admin OR email in
    # ADMIN_EMAILS` (the env allowlist is the bootstrap super-admin; this flag
    # is what the developer portal toggles per user).
    is_admin: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Set when an admin bans the account. Banned users are rejected at auth
    # (like soft-deleted users) but the row + reason are retained.
    banned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ban_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # ── Brute-force lockout ──
    # Consecutive failed password attempts; reset to 0 on success. Once it hits
    # the configured threshold, `locked_until` is stamped and sign-in is refused
    # until it passes (defends admin + all accounts from password guessing).
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ── Two-factor auth (TOTP) ──
    # Sealed TOTP secret ("f:<fernet>" when MFA_SECRET_KEY is set, else
    # "p:<base32>"). Set while enrolling and kept while MFA is on.
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    # One-time recovery codes, stored as a JSON list of argon2 hashes; each is
    # consumed (removed) on use so a leaked code can't be replayed.
    mfa_backup_codes: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ── Loupe Pro (subscription) ──
    # `free` | `pro`. Effective entitlements are computed in
    # `entitlement_service`, which also honours the `subscriptions_enabled`
    # kill switch (off => everyone treated as Pro). Never trust the client.
    plan: Mapped[str] = mapped_column(String(16), default="free", nullable=False)
    # When the user first became Pro (for "Pro since" / lifetime-value stats).
    pro_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True while the subscription is in its free trial (Stripe `trialing`).
    pro_trialing: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Null = no expiry (a comp / lifetime grant). Set to the period end once
    # Stripe is wired so a lapsed subscription auto-downgrades to free.
    pro_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stripe customer id (`cus_...`). Null until the user starts a checkout.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Active Stripe subscription id (`sub_...`). Set by the billing webhook;
    # used to drive the customer portal + reconcile lifecycle events.
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
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
