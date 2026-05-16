"""Endpoints for the signed-in user (``/me``, ``/me/settings``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.user import (
    UserRead,
    UserSettingsRead,
    UserSettingsUpdate,
    UserUpdate,
)
from app.services import user_service

router = APIRouter(prefix="/me", tags=["users"])


@router.get("", response_model=UserRead, summary="Get current user profile")
async def get_me(user: User = Depends(require_user)) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("", response_model=UserRead, summary="Update current user profile")
async def patch_me(
    payload: UserUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    updated = await user_service.update_profile(db, user, payload)
    return UserRead.model_validate(updated)


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
