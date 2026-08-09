"""Endpoints for the signed-in user (``/me``, ``/me/settings``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user, require_user
from app.db import get_db
from app.models.push_token import PushToken
from app.models.user import User
from app.models.user_recents import UserRecents
from app.platform.rate_limit import verify_email_limit
from app.schemas.entitlement import EntitlementsRead
from app.schemas.user import (
    UserRead,
    UserSettingsRead,
    UserSettingsUpdate,
    UserUpdate,
)
from app.services import billing_service, email_service, entitlement_service
from app.services.auth import email_verify_service, user_service
from app.utils import phone as phone_utils

router = APIRouter(prefix="/me", tags=["users"])


def _to_user_read(user: User) -> UserRead:
    """Serialize a user, stamping effective `is_admin` (DB grant or allowlist).

    The phone is MASKED here rather than in the schema so there is exactly
    one place the raw number could ever escape, and it doesn't.
    """
    out = UserRead.model_validate(user)
    out.is_admin = is_admin_user(user)
    out.phone = phone_utils.mask(user.phone)
    out.phone_verified = user.phone_verified_at is not None
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


class PushTokenRegister(BaseModel):
    """Body for ``POST /v1/me/push-tokens`` (device registration)."""

    token: str = Field(..., min_length=10, max_length=200)
    platform: str = Field(default="ios", pattern="^(ios|android)$")


class VerifyEmailResendResponse(BaseModel):
    sent: bool
    already_verified: bool = False


@router.post(
    "/push-tokens",
    status_code=204,
    summary="Register this device for push notifications",
)
async def register_push_token(
    payload: PushTokenRegister,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Idempotent upsert by token — a device that re-registers (or switches
    accounts) moves to the caller, never duplicates."""
    from sqlalchemy import select

    existing = (
        await db.execute(select(PushToken).where(PushToken.token == payload.token))
    ).scalar_one_or_none()
    if existing is None:
        db.add(
            PushToken(user_id=user.id, token=payload.token, platform=payload.platform)
        )
    else:
        existing.user_id = user.id
        existing.platform = payload.platform
    await db.commit()


@router.delete(
    "/push-tokens/{token}",
    status_code=204,
    summary="Unregister a device (sign-out)",
)
async def remove_push_token(
    token: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    from sqlalchemy import delete as sa_delete

    await db.execute(
        sa_delete(PushToken).where(
            PushToken.token == token, PushToken.user_id == user.id
        )
    )
    await db.commit()


@router.post(
    "/verify-email/resend",
    response_model=VerifyEmailResendResponse,
    summary="Re-send the email-confirmation link",
)
async def resend_verify_email(
    user: User = Depends(require_user),
    _throttle: None = Depends(verify_email_limit),
) -> VerifyEmailResendResponse:
    if user.email_verified_at is not None:
        return VerifyEmailResendResponse(sent=False, already_verified=True)
    sent = await email_service.send_verify_email(
        user, email_verify_service.verify_url(str(user.id))
    )
    return VerifyEmailResendResponse(sent=sent)


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


# ── Recents: cross-device recent searches + recently-viewed ──


class RecentsPayload(BaseModel):
    """A user's recents. ``viewed`` items are stored verbatim (the client's
    camelCase ``{id, name, imageUrl, setName, kind}``), so no field mapping
    is needed in either direction."""

    searches: list[str] = Field(default_factory=list)
    viewed: list[dict[str, Any]] = Field(default_factory=list)


@router.get(
    "/recents",
    response_model=RecentsPayload,
    summary="My recent searches + recently-viewed",
)
async def get_recents(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> RecentsPayload:
    row = await db.get(UserRecents, user.id)
    if row is None:
        return RecentsPayload()
    return RecentsPayload(searches=row.searches or [], viewed=row.viewed or [])


@router.put(
    "/recents",
    response_model=RecentsPayload,
    summary="Replace my recents (client sends the merged, capped list)",
)
async def put_recents(
    payload: RecentsPayload,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> RecentsPayload:
    # Defensive caps — the client already dedupes/caps, but never trust it.
    searches = [s for s in payload.searches if isinstance(s, str) and s.strip()][:50]
    viewed = [v for v in payload.viewed if isinstance(v, dict) and v.get("id")][:50]
    row = await db.get(UserRecents, user.id)
    if row is None:
        db.add(UserRecents(user_id=user.id, searches=searches, viewed=viewed))
    else:
        row.searches = searches
        row.viewed = viewed
    await db.commit()
    return RecentsPayload(searches=searches, viewed=viewed)


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


@router.post(
    "/billing/cancel",
    summary="Cancel my Loupe Pro subscription (keeps access until period end)",
)
async def billing_cancel(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Self-serve cancel — schedules ``cancel_at_period_end`` so the member
    keeps Pro until the paid period runs out. The plan downgrade itself is
    driven by the Stripe webhook, keeping Stripe the source of truth."""
    return await billing_service.cancel_subscription(db, user)


@router.post(
    "/billing/reactivate",
    summary="Undo a scheduled cancellation and keep Loupe Pro",
)
async def billing_reactivate(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Self-serve reactivate — clears a pending ``cancel_at_period_end`` so a
    member who changed their mind keeps Pro without re-checking out."""
    return await billing_service.reactivate_subscription(db, user)


__all__ = ["router"]
