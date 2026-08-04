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
from app.services import site_config_service
from app.utils.logger import get_logger

logger = get_logger("entitlement")

#: Default free card cap — the seeded value the admin can change live in the
#: portal (the live value is read from SiteConfig, never this constant).
FREE_CARD_LIMIT = 50

#: Default free statement allowance (also admin-tunable via SiteConfig).
FREE_STATEMENT_LIMIT = 1

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
    # SQLite drops tzinfo on round-trip; treat a naive timestamp as UTC so the
    # comparison never raises (Postgres keeps it aware).
    expires = user.pro_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC)


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
    """Compute the full entitlement payload the clients gate UI on.

    Reads the admin-tunable :class:`SiteConfig` for the live plan shape — the
    free limits and which features are actually gated — so flipping a feature
    free (or moving the cap) takes effect immediately with no deploy.
    """
    cfg = await site_config_service.get(db)
    enabled = await subscriptions_enabled(db)
    pro = (not enabled) or _plan_is_pro(user)

    def avail(gated: bool) -> bool:
        # A feature is available to you if you're Pro, or it isn't gated at all.
        return pro or (not gated)

    features = PlanFeatures(
        unlimited_cards=avail(cfg.gate_unlimited_cards),
        scanner_import=avail(cfg.gate_scanner_import),
        full_history=avail(cfg.gate_full_history),
        unlimited_alerts=avail(cfg.gate_unlimited_alerts),
        statements=avail(cfg.gate_statements),
        pro_badge=pro,
        ai_search=pro,
    )
    return EntitlementsRead(
        plan="pro" if pro else "free",
        is_pro=pro,
        trialing=pro and bool(user.pro_trialing),
        subscriptions_enabled=enabled,
        pro_since=user.pro_since,
        pro_expires_at=user.pro_expires_at,
        card_count=await count_cards(db, user),
        limits=PlanLimits(
            max_cards=None if features.unlimited_cards else cfg.free_card_limit,
            free_statements=None if features.statements else cfg.free_statement_limit,
        ),
        features=features,
    )


#: Free accounts may hold this many ACTIVE price alerts. A constant (not a
#: SiteConfig column) mirrors the paywall copy "a few alerts free"; promote it
#: to the admin plan panel if it ever needs live tuning.
FREE_ALERT_LIMIT = 5


async def enforce_can_add_alert(db: AsyncSession, user: User) -> None:
    """Raise 402 when a free account tries to exceed the alert allowance.

    No-op for Pro, when the kill switch is off, or when the alerts feature
    isn't gated. Structured ``detail`` (code=alert_limit_reached) lets the
    client open the paywall with the right story instead of a generic error.
    """
    if await is_pro(db, user):
        return
    cfg = await site_config_service.get(db)
    if not cfg.gate_unlimited_alerts:
        return
    from sqlalchemy import func, select

    from app.models.price_alert import PriceAlert

    count = (
        await db.execute(
            select(func.count(PriceAlert.id)).where(PriceAlert.user_id == user.id)
        )
    ).scalar_one()
    if count >= FREE_ALERT_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "alert_limit_reached",
                "limit": FREE_ALERT_LIMIT,
                "message": (
                    f"Free accounts can keep up to {FREE_ALERT_LIMIT} price "
                    "alerts. Upgrade to Loupe Pro for unlimited alerts."
                ),
            },
        )


async def enforce_ai_search(db: AsyncSession, user: User) -> None:
    """Raise 402 when a free account asks the AI search. Pure Pro gate — no
    free allowance, no SiteConfig column: the kill switch already promotes
    everyone to Pro when subscriptions are off. Structured ``detail``
    (code=ai_search_pro) opens the paywall with the right story."""
    if await is_pro(db, user):
        return
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "ai_search_pro",
            "message": (
                "AI search is a Loupe Pro feature. Upgrade to describe any "
                "card in your own words and let Loupe find it."
            ),
        },
    )


async def enforce_can_add_card(db: AsyncSession, user: User) -> None:
    """Raise 402 if adding one more card would exceed the free-tier cap.

    No-op when the user is Pro, the kill switch is off, the cards feature isn't
    gated, or no cap is set. The structured ``detail`` lets the client recognise
    the limit and open the paywall instead of showing a generic error.
    """
    if await is_pro(db, user):
        return
    cfg = await site_config_service.get(db)
    if not cfg.gate_unlimited_cards:
        return
    limit = cfg.free_card_limit
    if limit is None:
        return
    count = await count_cards(db, user)
    if count >= limit:
        await _notify_limit_reached(db, user, card_count=count, limit=limit)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "card_limit_reached",
                "limit": limit,
                "message": (
                    f"Free accounts can track up to {limit} cards. "
                    "Upgrade to Loupe Pro for unlimited."
                ),
            },
        )


async def _notify_limit_reached(
    db: AsyncSession, user: User, *, card_count: int, limit: int
) -> None:
    """Email the ceiling notice the first time a user is blocked by the cap.

    A full vault *stays* full, so this path runs on every subsequent add
    attempt — the delivery-log guard is what keeps it to one email. Failures
    here must never turn a clean 402 into a 500, so everything is swallowed.
    """
    from app.services import email_log_service, email_service

    try:
        already = await email_log_service.has_ever_sent(
            db, user.id, email_service.CATEGORY_VAULT_FULL
        )
        if not already:
            await email_service.send_free_limit_reached(
                user, card_count=card_count, limit=limit
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vault-full notice failed for user=%s (%s)", user.id, exc)


__all__ = [
    "FREE_ALERT_LIMIT",
    "FREE_CARD_LIMIT",
    "SUBSCRIPTIONS_FLAG",
    "count_cards",
    "enforce_ai_search",
    "enforce_can_add_alert",
    "enforce_can_add_card",
    "entitlements_for",
    "is_pro",
    "subscriptions_enabled",
]
