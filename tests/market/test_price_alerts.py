"""End-to-end tests for `/v1/alerts` and the worker hook."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.enums import PriceAlertCondition
from app.models.price_alert import PriceAlert
from app.services.market import price_alert_service
from tests.conftest import assert_envelope_error, assert_envelope_ok
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
async def test_create_alert_by_composite_id_in_card_id(
    client, db_session, created_user, auth_headers
):
    """Set an alert on a catalog card by its composite id passed in `card_id`.

    This is exactly what the mobile card-detail sends — the backend resolves +
    materializes it instead of rejecting the non-UUID, and echoes `upstream_id`
    so the client can match the "already has an alert?" state.
    """
    from app.services.catalog import card_resolver_service

    card = await make_card(db_session)
    await card_resolver_service.link_external_ref(
        db_session, card_id=card.id, source="pokemontcg", external_id="base1-99"
    )
    await db_session.commit()
    composite = "pokemontcg:base1-99"

    resp = await client.post(
        "/v1/alerts",
        json={"card_id": composite, "condition": "above", "threshold_usd": "10.00"},
        headers=auth_headers,
    )
    data = assert_envelope_ok(resp, expected_status=201)
    assert data["card_id"] == str(card.id)
    assert data["upstream_id"] == composite

    rows = assert_envelope_ok(await client.get("/v1/alerts", headers=auth_headers))
    assert rows[0]["upstream_id"] == composite


@pytest.mark.asyncio
async def test_delete_unknown_alert_returns_404(client, auth_headers):
    resp = await client.delete(f"/v1/alerts/{uuid.uuid4()}", headers=auth_headers)
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
async def test_create_by_upstream_id_materializes_local_card(
    client, db_session, auth_headers, monkeypatch
):
    """Web sends a composite ``<source>:<id>``; the service materializes it."""
    from app.services.catalog import card_search_service

    upstream_id = "pokemontcg:xy1-1"
    unified = {
        "id": upstream_id,
        "tcg": "pokemon",
        "name": "Materialized Charizard",
        "number": "1",
        "rarity": "Rare Holo",
        "year": 2014,
        "image_url": "https://example.test/charizard.png",
        "set": {"name": "Test Set", "releaseDate": "2014/02/05"},
        "set_name": "Test Set",
        "pricing_summary": {"market": {"amount": "120.00", "currency": "USD"}},
        "images": {"small": "https://example.test/s.png"},
        "attributes": {},
    }

    async def _fake_get_card(_cid: str) -> dict:
        return unified

    monkeypatch.setattr(card_search_service, "get_card", _fake_get_card)

    resp = await client.post(
        "/v1/alerts",
        json={
            "upstream_id": upstream_id,
            "condition": "below",
            "threshold_usd": "90.00",
        },
        headers=auth_headers,
    )
    data = assert_envelope_ok(resp, expected_status=201)
    assert data["card_name"] == "Materialized Charizard"
    assert data["condition"] == "below"
    # A real local card UUID was created + returned.
    uuid.UUID(data["card_id"])

    # Idempotent: a second alert on the same upstream id reuses the local card.
    resp2 = await client.post(
        "/v1/alerts",
        json={
            "upstream_id": upstream_id,
            "condition": "above",
            "threshold_usd": "200.00",
        },
        headers=auth_headers,
    )
    data2 = assert_envelope_ok(resp2, expected_status=201)
    assert data2["card_id"] == data["card_id"]


@pytest.mark.asyncio
async def test_create_requires_a_card_identifier(client, auth_headers):
    resp = await client.post(
        "/v1/alerts",
        json={"condition": "above", "threshold_usd": "5.00"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


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
