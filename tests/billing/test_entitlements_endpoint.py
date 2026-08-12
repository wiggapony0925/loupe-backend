"""HTTP contract for `GET /v1/me/entitlements` — what the client is allowed to do.

The clients never decide what's unlocked; they render this payload. So the
endpoint has to agree with the plan on the user row in every state: gating off,
free, paid, trialing, and lapsed. These are the same rules the service tests in
``test_entitlements_billing.py`` cover, asserted through the wire where the
apps actually read them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.feature_flag import FeatureFlag
from app.models.grade import GradedCard
from app.services import entitlement_service
from tests.conftest import assert_envelope_error, assert_envelope_ok

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


def _add_cards(db, user, count: int) -> None:
    for _ in range(count):
        db.add(GradedCard(user_id=user.id, card_id=uuid.uuid4(), grade=10, house="psa"))


async def test_entitlements_reject_anonymous_callers(client):
    assert_envelope_error(await client.get("/v1/me/entitlements"), expected_status=401)


async def test_entitlements_treat_everyone_as_pro_while_the_kill_switch_is_off(
    client, auth_headers, created_user
):
    """The kill switch is the billing outage plan: with subscriptions disabled
    the API must hand every caller an unlimited payload so no paywall renders."""
    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["subscriptions_enabled"] is False
    assert data["is_pro"] is True
    assert data["plan"] == "pro"
    assert data["limits"]["max_cards"] is None


async def test_entitlements_report_the_free_caps_once_gating_is_on(
    client, auth_headers, created_user, db_session
):
    await _enable_subscriptions(db_session)
    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["subscriptions_enabled"] is True
    assert data["is_pro"] is False
    assert data["plan"] == "free"
    assert data["limits"]["max_cards"] == entitlement_service.FREE_CARD_LIMIT
    assert data["features"]["unlimited_cards"] is False
    assert data["features"]["pro_badge"] is False
    assert data["features"]["ai_search"] is False


async def test_entitlements_unlock_everything_for_a_paid_member(
    client, auth_headers, created_user, db_session
):
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    created_user.pro_since = datetime.now(UTC) - timedelta(days=10)
    created_user.pro_expires_at = datetime.now(UTC) + timedelta(days=20)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["is_pro"] is True
    assert data["limits"] == {"max_cards": None, "free_statements": None}
    assert all(data["features"].values())
    assert data["pro_since"] is not None
    assert data["pro_expires_at"] is not None


async def test_entitlements_stop_at_the_expiry_date(
    client, auth_headers, created_user, db_session
):
    """A `pro` plan row is not a licence in perpetuity — access is only valid
    through `pro_expires_at`, so a lapsed member reads as free even if the
    downgrade webhook never arrived."""
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    created_user.pro_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["is_pro"] is False
    assert data["limits"]["max_cards"] == entitlement_service.FREE_CARD_LIMIT


async def test_entitlements_flag_an_active_trial(
    client, auth_headers, created_user, db_session
):
    """The apps show "trial ends in N days" off this flag; a trialing member is
    fully Pro in the meantime."""
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    created_user.pro_trialing = True
    created_user.pro_expires_at = datetime.now(UTC) + timedelta(days=7)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["is_pro"] is True
    assert data["trialing"] is True


async def test_a_lapsed_member_is_not_marked_as_trialing(
    client, auth_headers, created_user, db_session
):
    """`pro_trialing` can be left set on the row; the payload must not surface a
    trial to someone who has no access at all."""
    await _enable_subscriptions(db_session)
    created_user.plan = "pro"
    created_user.pro_trialing = True
    created_user.pro_expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["is_pro"] is False
    assert data["trialing"] is False


async def test_the_card_meter_counts_only_my_own_cards(
    client, auth_headers, created_user, second_user, db_session
):
    """`card_count` drives the "X of 50" meter and the upgrade nudge, so another
    tenant's vault must never push me toward a paywall."""
    await _enable_subscriptions(db_session)
    _add_cards(db_session, created_user, 3)
    _add_cards(db_session, second_user, 7)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert data["card_count"] == 3
