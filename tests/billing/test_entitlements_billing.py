"""Entitlements + Stripe billing — the plan-gating rules and webhook sync.

Stripe's network is never touched: we exercise the unconfigured "graceful"
paths and the webhook reconciliation logic (`_apply_subscription`) directly,
since that's a pure DB transform over a subscription object.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.feature_flag import FeatureFlag
from app.models.grade import GradedCard
from app.services import billing_service, entitlement_service

pytestmark = pytest.mark.asyncio


async def _enable_subscriptions(db) -> None:
    db.add(
        FeatureFlag(
            key=entitlement_service.SUBSCRIPTIONS_FLAG,
            label="Subscriptions",
            enabled=True,
        )
    )
    await db.commit()


async def _fill_to_cap(db, user) -> None:
    for _ in range(entitlement_service.FREE_CARD_LIMIT):
        db.add(GradedCard(user_id=user.id, card_id=uuid.uuid4(), grade=10, house="psa"))
    await db.commit()


# ── Kill switch ──────────────────────────────────────────────────────────


async def test_killswitch_off_treats_everyone_as_pro(db_session, created_user):
    ent = await entitlement_service.entitlements_for(db_session, created_user)
    assert ent.is_pro is True
    assert ent.subscriptions_enabled is False
    assert ent.limits.max_cards is None
    # Enforcement is a no-op while gating is off.
    await entitlement_service.enforce_can_add_card(db_session, created_user)


# ── Free-tier gating ─────────────────────────────────────────────────────


async def test_free_user_is_capped_when_subscriptions_on(db_session, created_user):
    await _enable_subscriptions(db_session)
    ent = await entitlement_service.entitlements_for(db_session, created_user)
    assert ent.is_pro is False
    assert ent.limits.max_cards == entitlement_service.FREE_CARD_LIMIT


async def test_enforce_raises_402_at_cap(db_session, created_user):
    await _enable_subscriptions(db_session)
    await _fill_to_cap(db_session, created_user)
    with pytest.raises(Exception) as exc:
        await entitlement_service.enforce_can_add_card(db_session, created_user)
    assert getattr(exc.value, "status_code", None) == 402
    assert exc.value.detail["code"] == "card_limit_reached"


async def test_pro_user_is_unlimited(db_session, created_user):
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    await db_session.commit()
    await _fill_to_cap(db_session, created_user)
    ent = await entitlement_service.entitlements_for(db_session, created_user)
    assert ent.is_pro is True
    assert ent.limits.max_cards is None
    await entitlement_service.enforce_can_add_card(db_session, created_user)  # no raise


async def test_expired_pro_downgrades_to_free(db_session, created_user):
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    created_user.pro_expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()
    ent = await entitlement_service.entitlements_for(db_session, created_user)
    assert ent.is_pro is False


# ── Billing — graceful when unconfigured ─────────────────────────────────


async def test_checkout_unavailable_without_stripe(db_session, created_user):
    res = await billing_service.start_checkout(db_session, created_user, "monthly")
    assert res["status"] == "unavailable"
    assert "message" in res


async def test_webhook_requires_signing_secret(db_session):
    with pytest.raises(Exception) as exc:
        billing_service.construct_event(b"{}", "t=1,v1=abc")
    assert getattr(exc.value, "status_code", None) == 503


# ── Webhook reconciliation (pure DB transform) ───────────────────────────


async def test_apply_subscription_grants_and_revokes_pro(db_session, created_user):
    created_user.stripe_customer_id = "cus_test_123"
    await db_session.commit()
    period_end = int((datetime.now(UTC) + timedelta(days=30)).timestamp())

    await billing_service._apply_subscription(
        db_session,
        {
            "id": "sub_1",
            "customer": "cus_test_123",
            "status": "active",
            "current_period_end": period_end,
        },
    )
    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_1"
    assert created_user.pro_since is not None
    assert created_user.pro_expires_at is not None

    await billing_service._apply_subscription(
        db_session,
        {
            "id": "sub_1",
            "customer": "cus_test_123",
            "status": "canceled",
            "current_period_end": period_end,
        },
    )
    await db_session.refresh(created_user)
    assert created_user.plan == "free"


async def test_apply_subscription_ignores_unknown_customer(db_session):
    # No user maps to this customer — must not raise.
    await billing_service._apply_subscription(
        db_session,
        {"id": "sub_x", "customer": "cus_nobody", "status": "active"},
    )
