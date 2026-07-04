"""User-account lifecycle: lookup, find-or-create, profile updates.

Supports four sign-in mechanisms:
- Email + password (``create_with_password`` / ``authenticate_with_password``)
- Sign in with Apple (``find_or_create_by_apple``)
- Sign in with Google (``find_or_create_by_google``)
- Dev login (``find_or_create_dev_user``) — gated by ``app_env`` at the router level.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.config import get_settings
from app.models.user import User, UserSettings
from app.schemas.user import UserSettingsUpdate, UserUpdate
from app.utils.logger import get_logger

logger = get_logger("services.user")


class AccountLockedError(Exception):
    """Raised when sign-in is refused because the account is temporarily locked
    after too many failed password attempts.

    Attributes:
        retry_after: seconds until the lock lifts (for the ``Retry-After`` header).
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__("Account temporarily locked")
        self.retry_after = retry_after


async def get_by_id(db: AsyncSession, user_id) -> User | None:
    return (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()


async def bump_token_version(db: AsyncSession, user: User) -> int:
    """Increment the user's token epoch → revoke every outstanding token.

    Powers "sign out everywhere" and is the kill switch for a stolen token; any
    future password-change flow should call this too. Returns the new version.
    """
    user.token_version = (user.token_version or 0) + 1
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Bumped token_version for user=%s → %s", user.id, user.token_version)
    return user.token_version


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()


async def active_emails(db: AsyncSession) -> list[str]:
    """Emails of active users (not deleted, not banned) — e.g. announcement lists."""
    rows = (
        await db.execute(
            select(User.email).where(
                User.deleted_at.is_(None), User.banned_at.is_(None)
            )
        )
    ).all()
    return [email for (email,) in rows if email]


async def announcement_recipients(db: AsyncSession) -> list[tuple[str, str]]:
    """``(user_id, email)`` of active users who accept announcement email.

    Like :func:`active_emails` but honors the per-user opt-out
    (``UserSettings.email_announcements_enabled``); users without a settings
    row default to subscribed. Use this — never ``active_emails`` — for
    blog/product-update blasts so unsubscribes stick.
    """
    rows = (
        await db.execute(
            select(User.id, User.email)
            .outerjoin(UserSettings, UserSettings.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
                (UserSettings.user_id.is_(None))
                | (UserSettings.email_announcements_enabled.is_(True)),
            )
        )
    ).all()
    return [(str(uid), email) for uid, email in rows if email]


async def get_by_apple_subject(db: AsyncSession, sub: str) -> User | None:
    return (
        await db.execute(select(User).where(User.apple_subject == sub))
    ).scalar_one_or_none()


async def get_by_google_subject(db: AsyncSession, sub: str) -> User | None:
    return (
        await db.execute(select(User).where(User.google_subject == sub))
    ).scalar_one_or_none()


async def _ensure_settings(db: AsyncSession, user: User) -> UserSettings:
    settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
        await db.flush()
    return settings


# ── Email + password ──────────────────────────────────────────────────────


class EmailAlreadyExistsError(Exception):
    """Raised when ``create_with_password`` is called for an existing email."""


class PasswordChangeError(Exception):
    """Raised when a password change can't proceed.

    ``reason`` is a stable code: ``"no_password"`` (SSO-only account, nothing to
    change) or ``"wrong_current"`` (the supplied current password didn't match).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def change_password(
    db: AsyncSession, user: User, *, current_password: str, new_password: str
) -> User:
    """Verify the current password, set the new one, and revoke all other sessions.

    Bumps ``token_version`` in the same commit, so every outstanding access +
    refresh token is invalidated — the caller re-issues a fresh pair for the
    current device so it stays signed in. Raises :class:`PasswordChangeError`
    for SSO-only accounts or a wrong current password.
    """
    if user.password_hash is None:
        raise PasswordChangeError("no_password")
    if not verify_password(current_password, user.password_hash):
        raise PasswordChangeError("wrong_current")
    user.password_hash = hash_password(new_password)
    user.token_version = (user.token_version or 0) + 1
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info("Password changed; sessions revoked for user=%s", user.id)
    return user


async def create_with_password(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
) -> User:
    """Create a new user authenticated by email + password."""
    normalised = email.lower()
    existing = await get_by_email(db, normalised)
    if existing is not None:
        raise EmailAlreadyExistsError(normalised)
    user = User(
        email=normalised,
        display_name=display_name,
        password_hash=hash_password(password),
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:  # race against another concurrent create
        await db.rollback()
        raise EmailAlreadyExistsError(normalised) from exc
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user via email/password: %s", user.id)
    return user


async def authenticate_with_password(
    db: AsyncSession, *, email: str, password: str
) -> User | None:
    """Return the user if the password matches; ``None`` otherwise (timing-safe).

    Enforces per-account brute-force lockout: after
    ``login_max_attempts`` consecutive failures the account is locked for
    ``login_lockout_seconds`` and this raises :class:`AccountLockedError`
    until the lock lifts. A correct password always resets the counter.
    """
    user = await get_by_email(db, email)
    # Hash a dummy value when the user is missing (or is SSO-only) so attackers
    # can't enumerate accounts by measuring response time.
    if user is None or user.password_hash is None:
        _ = verify_password(
            password, "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZXNhbHQ$ZmFrZWhhc2g"
        )
        return None

    settings = get_settings()
    now = datetime.now(UTC)

    # Already locked? Refuse before even checking the password.
    locked_until = _as_aware(user.locked_until)
    if locked_until is not None and locked_until > now:
        raise AccountLockedError(retry_after=int((locked_until - now).total_seconds()))

    if not verify_password(password, user.password_hash):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.login_max_attempts:
            user.locked_until = now + timedelta(seconds=settings.login_lockout_seconds)
            user.failed_login_count = 0  # reset; the lock is the deterrent now
            logger.warning(
                "account locked after failed logins user=%s email=%s",
                user.id,
                user.email,
            )
        await db.commit()
        return None

    # Success — clear any failure state and opportunistically upgrade the hash.
    if user.failed_login_count or user.locked_until is not None:
        user.failed_login_count = 0
        user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    await db.commit()
    return user


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; treat naive timestamps as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ── Dev login (no password) ───────────────────────────────────────────────


async def find_or_create_dev_user(
    db: AsyncSession, *, email: str, display_name: str | None = None
) -> User:
    """Find-or-create a user by email; the router gates this on ``app_env``."""
    normalised = email.lower()
    user = await get_by_email(db, normalised)
    if user is not None:
        return user
    user = User(email=normalised, display_name=display_name or normalised.split("@")[0])
    db.add(user)
    await db.flush()
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
    logger.info("Created new user via dev-login: %s", user.id)
    return user


# ── Apple ─────────────────────────────────────────────────────────────────


async def find_or_create_by_apple(
    db: AsyncSession,
    *,
    apple_sub: str,
    email: str | None,
    display_name: str | None,
) -> User:
    user = await get_by_apple_subject(db, apple_sub)
    if user is None and email:
        user = await get_by_email(db, email)
        if user is not None and user.apple_subject is None:
            user.apple_subject = apple_sub
    created = user is None
    if user is None:
        effective_email = (email or f"apple+{apple_sub}@users.loupe.app").lower()
        user = User(
            email=effective_email,
            display_name=display_name,
            apple_subject=apple_sub,
            # Apple verifies addresses before releasing them to us.
            email_verified_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
        logger.info("Created new user via Apple sign-in: %s", user.id)
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
    if created:
        # Same welcome the /register path sends; best-effort. Skip the
        # synthetic relay address minted when Apple hides the real email.
        from app.services import email_service

        if not user.email.endswith("@users.loupe.app"):
            await email_service.send_welcome(user)
    return user


# ── Google ────────────────────────────────────────────────────────────────


async def find_or_create_by_google(
    db: AsyncSession,
    *,
    google_sub: str,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
) -> User:
    user = await get_by_google_subject(db, google_sub)
    if user is None and email:
        user = await get_by_email(db, email)
        if user is not None and user.google_subject is None:
            user.google_subject = google_sub
    created = user is None
    if user is None:
        effective_email = (email or f"google+{google_sub}@users.loupe.app").lower()
        user = User(
            email=effective_email,
            display_name=display_name,
            avatar_url=avatar_url,
            google_subject=google_sub,
            # Google verifies addresses before releasing them to us.
            email_verified_at=datetime.now(UTC),
        )
        db.add(user)
        await db.flush()
        logger.info("Created new user via Google sign-in: %s", user.id)
    elif avatar_url and not user.avatar_url:
        user.avatar_url = avatar_url
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
    if created:
        from app.services import email_service

        if not user.email.endswith("@users.loupe.app"):
            await email_service.send_welcome(user)
    return user


# ── Profile / settings ────────────────────────────────────────────────────


async def update_profile(db: AsyncSession, user: User, patch: UserUpdate) -> User:
    if patch.display_name is not None:
        user.display_name = patch.display_name
    if patch.avatar_url is not None:
        user.avatar_url = patch.avatar_url
    await db.commit()
    await db.refresh(user)
    return user


async def update_settings(
    db: AsyncSession, user: User, patch: UserSettingsUpdate
) -> UserSettings:
    settings = await _ensure_settings(db, user)
    if patch.currency is not None:
        settings.currency = patch.currency
    if patch.theme is not None:
        settings.theme = patch.theme
    if patch.live_sync_enabled is not None:
        settings.live_sync_enabled = patch.live_sync_enabled
    if patch.push_notifications_enabled is not None:
        settings.push_notifications_enabled = patch.push_notifications_enabled
    if patch.email_announcements_enabled is not None:
        settings.email_announcements_enabled = patch.email_announcements_enabled
    await db.commit()
    await db.refresh(settings)
    return settings


__all__ = [
    "AccountLockedError",
    "EmailAlreadyExistsError",
    "authenticate_with_password",
    "create_with_password",
    "find_or_create_by_apple",
    "find_or_create_by_google",
    "find_or_create_dev_user",
    "get_by_apple_subject",
    "get_by_email",
    "get_by_google_subject",
    "get_by_id",
    "update_profile",
    "update_settings",
]
