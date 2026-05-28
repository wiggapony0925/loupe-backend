"""End-to-end tests for `/v1/watchlist`."""

from __future__ import annotations

import uuid

import pytest

from app.services.collection import watchlist_service
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card


@pytest.mark.asyncio
async def test_add_list_delete_watchlist_roundtrip(
    client, db_session, created_user, auth_headers
):
    card = await make_card(db_session)

    # List is empty initially.
    resp = await client.get("/v1/watchlist", headers=auth_headers)
    assert assert_envelope_ok(resp) == []

    # POST pins the card.
    resp = await client.post(
        "/v1/watchlist",
        json={"card_id": str(card.id)},
        headers=auth_headers,
    )
    data = assert_envelope_ok(resp, expected_status=201)
    assert data["card_id"] == str(card.id)
    assert data["card_name"] == card.name
    pin_id = data["id"]

    # GET surfaces the pinned row.
    resp = await client.get("/v1/watchlist", headers=auth_headers)
    rows = assert_envelope_ok(resp)
    assert len(rows) == 1
    assert rows[0]["id"] == pin_id

    # DELETE by card_id removes it.
    resp = await client.delete(
        f"/v1/watchlist/{card.id}", headers=auth_headers
    )
    assert resp.status_code == 204

    resp = await client.get("/v1/watchlist", headers=auth_headers)
    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_add_is_idempotent(
    client, db_session, created_user, auth_headers
):
    card = await make_card(db_session)

    resp1 = await client.post(
        "/v1/watchlist",
        json={"card_id": str(card.id)},
        headers=auth_headers,
    )
    data1 = assert_envelope_ok(resp1, expected_status=201)

    # Second POST should return the same row, not duplicate.
    resp2 = await client.post(
        "/v1/watchlist",
        json={"card_id": str(card.id)},
        headers=auth_headers,
    )
    data2 = assert_envelope_ok(resp2, expected_status=201)
    assert data1["id"] == data2["id"]

    resp = await client.get("/v1/watchlist", headers=auth_headers)
    rows = assert_envelope_ok(resp)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_delete_unknown_card_returns_404(client, auth_headers):
    resp = await client.delete(
        f"/v1/watchlist/{uuid.uuid4()}", headers=auth_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_watchlist_requires_auth(client):
    resp = await client.get("/v1/watchlist")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_is_watching_helper(db_session, created_user):
    card = await make_card(db_session)

    assert not await watchlist_service.is_watching(
        db_session, created_user, card.id
    )

    await watchlist_service.add(db_session, created_user, card.id)
    assert await watchlist_service.is_watching(
        db_session, created_user, card.id
    )

    await watchlist_service.remove(db_session, created_user, card.id)
    assert not await watchlist_service.is_watching(
        db_session, created_user, card.id
    )
