"""Endpoints for the signed-in user (``/me``, ``/me/settings``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.config import get_settings as get_app_settings
from app.db import get_db
from app.models.user import User
from app.schemas.user import (
    UserRead,
    UserSettingsRead,
    UserSettingsUpdate,
    UserUpdate,
)
from app.services.auth import user_service

router = APIRouter(prefix="/me", tags=["users"])


def _to_user_read(user: User) -> UserRead:
    """Serialize a user, stamping `is_admin` from the email allowlist."""
    out = UserRead.model_validate(user)
    email = (user.email or "").strip().lower()
    out.is_admin = bool(email) and email in get_app_settings().admin_email_set
    return out


@router.get("", response_model=UserRead, summary="Get current user profile")
async def get_me(user: User = Depends(require_user)) -> UserRead:
    return _to_user_read(user)


@router.patch("", response_model=UserRead, summary="Update current user profile")
async def patch_me(
    payload: UserUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    updated = await user_service.update_profile(db, user, payload)
    return _to_user_read(updated)


@router.get("/settings", response_model=UserSettingsRead, summary="Get user settings")
async def get_settings(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> UserSettingsRead:
    settings = await user_service.update_settings(db, user, UserSettingsUpdate())
    return UserSettingsRead.model_validate(settings)


@router.patch(
    "/settings", response_model=UserSettingsRead, summary="Update user settings"
)
async def patch_settings(
    payload: UserSettingsUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UserSettingsRead:
    settings = await user_service.update_settings(db, user, payload)
    return UserSettingsRead.model_validate(settings)


__all__ = ["router"]
