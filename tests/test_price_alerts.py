"""End-to-end tests for `/v1/alerts` and the worker hook."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.enums import PriceAlertCondition
from app.models.price_alert import PriceAlert
from app.services import price_alert_service
from tests.conftest import assert_envelope_ok, assert_envelope_error
from tests.factories import make_card


@pytest.mark.asyncio
async def test_create_list_delete_alert_roundtrip(
    client, db_session, created_user, auth_headers
):
    card = await make_card(db_session)

    payload = {
        "card_id": str(card.id),
        "condition": "above",
        "threshold_usd": "12.50",
        "note": "buy back if it spikes",
    }
    resp = await client.post("/v1/alerts", json=payload, headers=auth_headers)
    data = assert_envelope_ok(resp, expected_status=201)
    alert_id = data["id"]
    assert data["condition"] == "above"
    assert data["card_id"] == str(card.id)
    assert data["card_name"] == card.name
    assert data["triggered_at"] is None

    resp = await client.get("/v1/alerts", headers=auth_headers)
    rows = assert_envelope_ok(resp)
    assert len(rows) == 1
    assert rows[0]["id"] == alert_id

    resp = await client.delete(f"/v1/alerts/{alert_id}", headers=auth_headers)
    assert resp.status_code == 204

    resp = await client.get("/v1/alerts", headers=auth_headers)
    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_delete_unknown_alert_returns_404(client, auth_headers):
    resp = await client.delete(
        f"/v1/alerts/{uuid.uuid4()}", headers=auth_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_alerts_require_auth(client):
    resp = await client.get("/v1/alerts")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_evaluate_for_card_fires_only_matching_pending_rows(
    db_session, created_user
):
    card = await make_card(db_session)

    above = PriceAlert(
        user_id=created_user.id,
        card_id=card.id,
        condition=PriceAlertCondition.above,
        threshold_usd=Decimal("10.00"),
    )
    below = PriceAlert(
        user_id=created_user.id,
        card_id=card.id,
        condition=PriceAlertCondition.below,
        threshold_usd=Decimal("5.00"),
    )
    not_yet = PriceAlert(
        user_id=created_user.id,
        card_id=card.id,
        condition=PriceAlertCondition.above,
        threshold_usd=Decimal("999.00"),
    )
    db_session.add_all([above, below, not_yet])
    await db_session.commit()

    fired = await price_alert_service.evaluate_for_card(
        db_session, card.id, Decimal("12.00")
    )
    await db_session.commit()

    assert {a.id for a in fired} == {above.id}
    await db_session.refresh(above)
    await db_session.refresh(below)
    await db_session.refresh(not_yet)
    assert above.triggered_at is not None
    assert above.triggered_price_usd == Decimal("12.00")
    assert below.triggered_at is None
    assert not_yet.triggered_at is None

    # Idempotent: already-fired rows are skipped on subsequent calls.
    fired_again = await price_alert_service.evaluate_for_card(
        db_session, card.id, Decimal("999999.00")
    )
    assert above not in fired_again


@pytest.mark.asyncio
async def test_evaluate_for_card_below_threshold(db_session, created_user):
    card = await make_card(db_session)
    alert = PriceAlert(
        user_id=created_user.id,
        card_id=card.id,
        condition=PriceAlertCondition.below,
        threshold_usd=Decimal("20.00"),
    )
    db_session.add(alert)
    await db_session.commit()

    fired = await price_alert_service.evaluate_for_card(
        db_session, card.id, Decimal("19.99")
    )
    assert [a.id for a in fired] == [alert.id]


@pytest.mark.asyncio
async def test_create_validates_positive_threshold(client, db_session, auth_headers):
    card = await make_card(db_session)
    resp = await client.post(
        "/v1/alerts",
        json={
            "card_id": str(card.id),
            "condition": "above",
            "threshold_usd": "0",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
