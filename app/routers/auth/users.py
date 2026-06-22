"""Endpoints for the signed-in user (``/me``, ``/me/settings``)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user, require_user
from app.db import get_db
from app.models.user import User
from app.schemas.entitlement import EntitlementsRead
from app.schemas.user import (
    UserRead,
    UserSettingsRead,
    UserSettingsUpdate,
    UserUpdate,
)
from app.services import billing_service, entitlement_service
from app.services.auth import user_service

router = APIRouter(prefix="/me", tags=["users"])


def _to_user_read(user: User) -> UserRead:
    """Serialize a user, stamping effective `is_admin` (DB grant or allowlist)."""
    out = UserRead.model_validate(user)
    out.is_admin = is_admin_user(user)
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


# ── Loupe Pro: entitlements + billing ──


@router.get(
    "/entitlements",
    response_model=EntitlementsRead,
    summary="The signed-in user's effective Loupe Pro access",
)
async def get_entitlements(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> EntitlementsRead:
    return await entitlement_service.entitlements_for(db, user)


class CheckoutRequest(BaseModel):
    """Start a Pro checkout for the given billing interval."""

    interval: str = Field("monthly", pattern="^(monthly|yearly)$")


@router.get("/billing/config", summary="Pricing + checkout availability")
async def get_billing_config() -> dict:
    return billing_service.public_config()


@router.post("/billing/checkout", summary="Begin a hosted Loupe Pro checkout")
async def start_checkout(
    payload: CheckoutRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await billing_service.start_checkout(db, user, payload.interval)


@router.post(
    "/billing/subscribe",
    summary="Create a subscription for the in-app Payment Element",
)
async def subscribe(
    payload: CheckoutRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await billing_service.create_subscription(db, user, payload.interval)


@router.post(
    "/billing/portal",
    summary="Open the Stripe customer portal (manage/cancel Pro)",
)
async def billing_portal(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await billing_service.create_portal_session(db, user)


__all__ = ["router"]
