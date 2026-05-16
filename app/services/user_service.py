"""User-account lifecycle: lookup, find-or-create, profile updates."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserSettings
from app.schemas.user import UserSettingsUpdate, UserUpdate
from app.utils.logger import get_logger

logger = get_logger("services.user")


async def get_by_id(db: AsyncSession, user_id) -> User | None:
    return (
        await db.execute(select(User).where(User.id == user_id))
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


async def find_or_create_by_apple(
    db: AsyncSession,
    *,
    apple_sub: str,
    email: str | None,
    display_name: str | None,
) -> User:
    """Look up a user by Apple subject, creating one (and settings) if missing."""
    user = await get_by_apple_subject(db, apple_sub)
    if user is None and email:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is not None and user.apple_subject is None:
            user.apple_subject = apple_sub
    if user is None:
        effective_email = email or f"apple+{apple_sub}@users.loupe.app"
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


async def find_or_create_by_google(
    db: AsyncSession,
    *,
    google_sub: str,
    email: str | None,
    display_name: str | None,
    avatar_url: str | None,
) -> User:
    """Look up a user by Google subject, creating one (and settings) if missing."""
    user = await get_by_google_subject(db, google_sub)
    if user is None and email:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is not None and user.google_subject is None:
            user.google_subject = google_sub
    if user is None:
        effective_email = email or f"google+{google_sub}@users.loupe.app"
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


async def update_profile(db: AsyncSession, user: User, patch: UserUpdate) -> User:
    """Apply allowed mutations to a user record."""
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
    """Apply allowed mutations to a user's settings."""
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
    "find_or_create_by_apple",
    "find_or_create_by_google",
    "get_by_apple_subject",
    "get_by_google_subject",
    "get_by_id",
    "update_profile",
    "update_settings",
]
