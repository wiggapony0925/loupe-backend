"""User-account lifecycle: lookup, find-or-create, profile updates.

Supports four sign-in mechanisms:
- Email + password (``create_with_password`` / ``authenticate_with_password``)
- Sign in with Apple (``find_or_create_by_apple``)
- Sign in with Google (``find_or_create_by_google``)
- Dev login (``find_or_create_dev_user``) — gated by ``app_env`` at the router level.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.models.user import User, UserSettings
from app.schemas.user import UserSettingsUpdate, UserUpdate
from app.utils.logger import get_logger

logger = get_logger("services.user")


async def get_by_id(db: AsyncSession, user_id) -> User | None:
    return (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()


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
    """Return the user if password matches; ``None`` otherwise (timing-safe)."""
    user = await get_by_email(db, email)
    # Hash a dummy value when the user is missing so attackers can't enumerate
    # accounts by measuring response time.
    if user is None or user.password_hash is None:
        _ = verify_password(
            password, "$argon2id$v=19$m=65536,t=3,p=4$ZmFrZXNhbHQ$ZmFrZWhhc2g"
        )
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await db.commit()
    return user


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
    if user is None:
        effective_email = (email or f"apple+{apple_sub}@users.loupe.app").lower()
        user = User(
            email=effective_email,
            display_name=display_name,
            apple_subject=apple_sub,
        )
        db.add(user)
        await db.flush()
        logger.info("Created new user via Apple sign-in: %s", user.id)
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
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
    if user is None:
        effective_email = (email or f"google+{google_sub}@users.loupe.app").lower()
        user = User(
            email=effective_email,
            display_name=display_name,
            avatar_url=avatar_url,
            google_subject=google_sub,
        )
        db.add(user)
        await db.flush()
        logger.info("Created new user via Google sign-in: %s", user.id)
    elif avatar_url and not user.avatar_url:
        user.avatar_url = avatar_url
    await _ensure_settings(db, user)
    await db.commit()
    await db.refresh(user)
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
    await db.commit()
    await db.refresh(settings)
    return settings


__all__ = [
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
