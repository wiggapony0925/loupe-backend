"""Free-tier price-alert cap — 402 with a structured code at the limit."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.models.feature_flag import FeatureFlag
from app.models.price_alert import PriceAlert
from app.models.site_config import SiteConfig
from app.models.user import User
from app.services import entitlement_service, site_config_service
from tests.factories import make_card


async def _mk_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"alerts-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Alerts",
    )
    db.add(user)
    await db.commit()
    return user


async def _gate_on(db) -> None:
    db.add(
        FeatureFlag(
            key=entitlement_service.SUBSCRIPTIONS_FLAG,
            label="Subscriptions",
            enabled=True,
        )
    )
    await site_config_service.get(db)  # ensure the singleton row exists
    await db.execute(update(SiteConfig).values(gate_unlimited_alerts=True))
    await db.commit()


@pytest.mark.anyio
async def test_free_user_blocked_at_alert_cap(db_session):
    await _gate_on(db_session)
    user = await _mk_user(db_session)
    for _ in range(entitlement_service.FREE_ALERT_LIMIT):
        card = await make_card(db_session, name="Alert Card")
        db_session.add(
            PriceAlert(
                user_id=user.id,
                card_id=card.id,
                threshold_usd=Decimal("10.00"),
                condition="above",
            )
        )
    await db_session.commit()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await entitlement_service.enforce_can_add_alert(db_session, user)
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "alert_limit_reached"


@pytest.mark.anyio
async def test_under_cap_is_allowed(db_session):
    await _gate_on(db_session)
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Solo Card")
    db_session.add(
        PriceAlert(
            user_id=user.id,
            card_id=card.id,
            threshold_usd=Decimal("5.00"),
            condition="above",
        )
    )
    await db_session.commit()
    # No raise expected.
    await entitlement_service.enforce_can_add_alert(db_session, user)


@pytest.mark.anyio
async def test_pro_user_is_never_capped(db_session, monkeypatch):
    await _gate_on(db_session)
    user = await _mk_user(db_session)

    async def always_pro(db, u):
        return True

    monkeypatch.setattr(entitlement_service, "is_pro", always_pro)
    for _ in range(entitlement_service.FREE_ALERT_LIMIT + 2):
        card = await make_card(db_session, name="Pro Alert Card")
        db_session.add(
            PriceAlert(
                user_id=user.id,
                card_id=card.id,
                threshold_usd=Decimal("10.00"),
                condition="above",
            )
        )
    await db_session.commit()
    await entitlement_service.enforce_can_add_alert(db_session, user)
