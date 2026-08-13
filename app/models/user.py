"""User and UserSettings ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UuidCol


class User(Base):
    """An end-user authenticated via Apple or Google sign-in."""

    __tablename__ = "users"
    __table_args__ = (
        # `plan` is written straight onto the row by the admin endpoint and
        # read by entitlement_service, which treats anything that is not "pro"
        # as free. So a typo does not fail — it silently downgrades a paying
        # customer, and no read path would ever surface the bad value. The
        # pydantic Literal already refuses it at the API edge; this is the same
        # rule for the writers that do not go through the API (a psql fix-up, a
        # billing backfill, a restore).
        CheckConstraint("plan IN ('free', 'pro')", name="ck_user_plan"),
    )

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
    #
    # `default` and `server_default` both, here and on the five columns below.
    # The Python default is what the ORM uses and is unchanged; the server
    # default is for every writer that is not the ORM — a psql backfill, a
    # data-repair script, a `COPY` restore — which until now hit a bare 23502
    # not-null violation on a column that has an obvious right answer. The
    # trade is that the two defaults can drift apart silently; they are spelled
    # next to each other to make that as visible as it can be.
    is_admin: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )
    # Set when an admin bans the account. Banned users are rejected at auth
    # (like soft-deleted users) but the row + reason are retained.
    # When the user proved they own their email (clicked the signed link, or
    # arrived via Apple/Google which verify addresses upstream). NULL =
    # unverified; nothing is gated on it yet — it's trust signal + a nudge.
    #: E.164 ("+14155550123"). Normalised on write so one person can't hold
    #: two rows for the same line, and UNIQUE for the same reason email is —
    #: it is an identity, and a future SMS login or 2FA needs it to resolve
    #: to exactly one account. NULL until the user supplies it.
    #:
    #: NEVER leaves the server on a public payload. It is absent from every
    #: social schema on purpose (see SocialUserCard / SocialProfileView):
    #: a directory that hands out phone numbers is a harvesting tool.
    phone: Mapped[str | None] = mapped_column(
        String(20), unique=True, index=True, nullable=True
    )
    #: Set once an OTP has actually been confirmed. Deliberately separate
    #: from `phone` being present: a number someone typed is a claim, and
    #: treating an unverified claim as proof is how account recovery gets
    #: abused. Nothing security-sensitive may key off `phone` alone.
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    banned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ban_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # ── Brute-force lockout ──
    # Consecutive failed password attempts; reset to 0 on success. Once it hits
    # the configured threshold, `locked_until` is stamped and sign-in is refused
    # until it passes (defends admin + all accounts from password guessing).
    failed_login_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ── Session revocation ──
    # Monotonic "token epoch". Every issued access/refresh token carries the
    # value current at sign-in as a ``ver`` claim; bumping this column instantly
    # invalidates *all* of a user's outstanding tokens (the "sign out everywhere"
    # / "revoke a stolen token" kill switch). Bumped by /auth/logout-all and any
    # future password-change. Stateless: no per-token store to maintain.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # ── Two-factor auth (TOTP) ──
    # Sealed TOTP secret ("f:<fernet>" when MFA_SECRET_KEY is set, else
    # "p:<base32>"). Set while enrolling and kept while MFA is on.
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )
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
    plan: Mapped[str] = mapped_column(
        String(16), default="free", server_default="free", nullable=False
    )
    # When the user first became Pro (for "Pro since" / lifetime-value stats).
    pro_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # True while the subscription is in its free trial (Stripe `trialing`).
    pro_trialing: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )
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

    @property
    def email_verified(self) -> bool:
        """Read by ``UserRead`` (pydantic ``from_attributes``)."""
        return self.email_verified_at is not None


class UserSettings(Base):
    """Per-user app preferences (theme, currency, sync flags)."""

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UuidCol(), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # ISO-4217 fiat codes plus crypto tickers (BTC, USDC, MATIC…) — the
    # clients share one display-currency catalog with 2-6 char codes.
    currency: Mapped[str] = mapped_column(String(6), default="USD", nullable=False)
    # Active portfolio (collection) the user is viewing — scopes the dashboard,
    # analytics and vault. NULL = the "All" view (everything owned). Stored on
    # the profile like `currency` so the choice follows the user across mobile
    # and web. Not an FK: a stale id just falls back to All, and this survives a
    # collection being deleted without a cascade dance.
    active_collection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    theme: Mapped[str] = mapped_column(String(16), default="system", nullable=False)
    live_sync_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    push_notifications_enabled: Mapped[bool] = mapped_column(
        default=True, nullable=False
    )
    # Announcement-class email (blog posts, product updates). Transactional
    # mail (security notices, receipts, alerts they created) is NOT gated on
    # this — only the marketing-ish blasts, per CAN-SPAM.
    email_announcements_enabled: Mapped[bool] = mapped_column(
        default=True, server_default=true(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship("User", back_populates="settings")


__all__ = ["User", "UserSettings"]
