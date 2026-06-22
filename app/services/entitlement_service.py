"""Loupe Pro entitlements — the one place that decides what a user can do.

Two inputs feed every decision:

* the user's ``plan`` (free|pro, honouring ``pro_expires_at``), and
* the global ``subscriptions_enabled`` feature flag (the kill switch).

When the kill switch is **off**, everyone is treated as Pro — no limits, no
paywall. That's the safe default: a billing outage (or a launch we want to
walk back) is a single portal toggle away from "everything free", with no
deploy and no code change. Routers and the ``/me/entitlements`` endpoint both
call through here so the rule lives in exactly one spot.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_flag import FeatureFlag
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.entitlement import EntitlementsRead, PlanFeatures, PlanLimits

#: The free tier may track this many cards. Pro is unlimited.
FREE_CARD_LIMIT = 50

#: Feature-flag key for the global subscriptions kill switch.
SUBSCRIPTIONS_FLAG = "subscriptions_enabled"


async def subscriptions_enabled(db: AsyncSession) -> bool:
    """Whether gating is active. Missing/unknown flag => off (safe default)."""
    enabled = (
        await db.execute(
            select(FeatureFlag.enabled).where(FeatureFlag.key == SUBSCRIPTIONS_FLAG)
        )
    ).scalar_one_or_none()
    return bool(enabled)


def _plan_is_pro(user: User) -> bool:
    """A user's own Pro status, ignoring the global switch (honours expiry)."""
    if user.plan != "pro":
        return False
    if user.pro_expires_at is None:
        return True
    return user.pro_expires_at > datetime.now(UTC)


async def is_pro(db: AsyncSession, user: User) -> bool:
    """Effective Pro: the kill switch promotes everyone when gating is off."""
    if not await subscriptions_enabled(db):
        return True
    return _plan_is_pro(user)


async def count_cards(db: AsyncSession, user: User) -> int:
    """Live count of the user's owned (non-deleted) cards."""
    total = (
        await db.execute(
            select(func.count(GradedCard.id)).where(
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return int(total or 0)


async def entitlements_for(db: AsyncSession, user: User) -> EntitlementsRead:
    """Compute the full entitlement payload the clients gate UI on."""
    enabled = await subscriptions_enabled(db)
    pro = (not enabled) or _plan_is_pro(user)
    return EntitlementsRead(
        plan="pro" if pro else "free",
        is_pro=pro,
        subscriptions_enabled=enabled,
        pro_since=user.pro_since,
        pro_expires_at=user.pro_expires_at,
        card_count=await count_cards(db, user),
        limits=PlanLimits(max_cards=None if pro else FREE_CARD_LIMIT),
        features=PlanFeatures(
            unlimited_cards=pro,
            scanner_import=pro,
            full_history=pro,
            unlimited_alerts=pro,
            statements=pro,
            pro_badge=pro,
        ),
    )


async def enforce_can_add_card(db: AsyncSession, user: User) -> None:
    """Raise 402 if adding one more card would exceed the free-tier cap.

    No-op when the user is Pro or the kill switch is off. The structured
    ``detail`` lets the client recognise the limit and open the paywall
    instead of showing a generic error.
    """
    if await is_pro(db, user):
        return
    if await count_cards(db, user) >= FREE_CARD_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "card_limit_reached",
                "limit": FREE_CARD_LIMIT,
                "message": (
                    f"Free accounts can track up to {FREE_CARD_LIMIT} cards. "
                    "Upgrade to Loupe Pro for unlimited."
                ),
            },
        )


__all__ = [
    "FREE_CARD_LIMIT",
    "SUBSCRIPTIONS_FLAG",
    "count_cards",
    "enforce_can_add_card",
    "entitlements_for",
    "is_pro",
    "subscriptions_enabled",
]
