"""Price alerts actually FIRE — the notification pipeline is wired.

`evaluate_for_card` + `send_price_alert_push` existed but had no caller
(the nightly worker is offline in prod). `record_price_observation` now
evaluates pending alerts on every live-price observation and fans out
Expo pushes. These prove the trip + the push call, with the network leg
stubbed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.enums import PriceAlertCondition
from app.models.price_alert import PriceAlert
from app.models.user import User
from app.services import push_service
from app.tasks import price_snapshot
from tests.factories import make_card


async def _mk_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"alertfire-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="AlertFire",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.mark.anyio
async def test_observation_trips_alert_and_pushes(db_session, monkeypatch):
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Charizard")
    db_session.add(
        PriceAlert(
            user_id=user.id,
            card_id=card.id,
            condition=PriceAlertCondition.above,
            threshold_usd=Decimal("100.00"),
        )
    )
    await db_session.commit()

    pushes: list[dict] = []

    async def fake_push(user_id, **kw):
        pushes.append({"user_id": user_id, **kw})
        return 1

    monkeypatch.setattr(push_service, "send_price_alert_push", fake_push)

    # Observe a price ABOVE the threshold → alert trips + one push.
    await price_snapshot.record_price_observation(str(card.id), 150.0)

    row = (
        await db_session.execute(
            select(PriceAlert).where(PriceAlert.card_id == card.id)
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.triggered_at is not None, "alert must transition to triggered"
    assert float(row.triggered_price_usd) == 150.0
    assert len(pushes) == 1
    assert pushes[0]["card_name"] == "Charizard"
    assert pushes[0]["threshold_usd"] == 100.0


@pytest.mark.anyio
async def test_price_below_threshold_does_not_fire(db_session, monkeypatch):
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Blastoise")
    db_session.add(
        PriceAlert(
            user_id=user.id,
            card_id=card.id,
            condition=PriceAlertCondition.above,
            threshold_usd=Decimal("100.00"),
        )
    )
    await db_session.commit()

    pushes: list[dict] = []

    async def fake_push(user_id, **kw):
        pushes.append(kw)
        return 1

    monkeypatch.setattr(push_service, "send_price_alert_push", fake_push)

    await price_snapshot.record_price_observation(str(card.id), 50.0)

    row = (
        await db_session.execute(
            select(PriceAlert).where(PriceAlert.card_id == card.id)
        )
    ).scalar_one()
    assert row.triggered_at is None
    assert pushes == []


@pytest.mark.anyio
async def test_already_triggered_alert_not_refired(db_session, monkeypatch):
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Venusaur")
    from datetime import UTC, datetime

    db_session.add(
        PriceAlert(
            user_id=user.id,
            card_id=card.id,
            condition=PriceAlertCondition.above,
            threshold_usd=Decimal("100.00"),
            triggered_at=datetime.now(UTC),
            triggered_price_usd=Decimal("120.00"),
        )
    )
    await db_session.commit()

    pushes: list[dict] = []

    async def fake_push(user_id, **kw):
        pushes.append(kw)
        return 1

    monkeypatch.setattr(push_service, "send_price_alert_push", fake_push)

    await price_snapshot.record_price_observation(str(card.id), 200.0)

    assert pushes == [], "a triggered alert must not re-fire"
