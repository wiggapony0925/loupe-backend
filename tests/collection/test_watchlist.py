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
    resp = await client.delete(f"/v1/watchlist/{card.id}", headers=auth_headers)
    assert resp.status_code == 204

    resp = await client.get("/v1/watchlist", headers=auth_headers)
    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_add_is_idempotent(client, db_session, created_user, auth_headers):
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
async def test_pin_by_composite_upstream_id_materializes(
    client, db_session, created_user, auth_headers
):
    """Heart a catalog card by its composite upstream id (browse/search view).

    The backend resolves + materializes it, so the client never has to know the
    local UUID — and the row comes back with `upstream_id` so the client can
    match its "is pinned?" state by the id it *does* have.
    """
    from app.services.catalog import card_resolver_service

    card = await make_card(db_session)
    await card_resolver_service.link_external_ref(
        db_session, card_id=card.id, source="pokemontcg", external_id="base1-4"
    )
    await db_session.commit()
    composite = "pokemontcg:base1-4"

    # POST with the composite id — not a UUID.
    resp = await client.post(
        "/v1/watchlist", json={"card_id": composite}, headers=auth_headers
    )
    data = assert_envelope_ok(resp, expected_status=201)
    assert data["card_id"] == str(card.id)  # resolved to the local card
    assert data["upstream_id"] == composite

    # List surfaces the composite id for client-side matching.
    rows = assert_envelope_ok(await client.get("/v1/watchlist", headers=auth_headers))
    assert len(rows) == 1
    assert rows[0]["upstream_id"] == composite

    # DELETE by the composite id works too.
    resp = await client.delete(f"/v1/watchlist/{composite}", headers=auth_headers)
    assert resp.status_code == 204
    assert (
        assert_envelope_ok(await client.get("/v1/watchlist", headers=auth_headers))
        == []
    )


@pytest.mark.asyncio
async def test_pin_unresolvable_id_returns_422(client, auth_headers):
    resp = await client.post(
        "/v1/watchlist",
        json={"card_id": "pokemontcg:does-not-exist"},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_delete_unknown_card_returns_404(client, auth_headers):
    resp = await client.delete(f"/v1/watchlist/{uuid.uuid4()}", headers=auth_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_watchlist_requires_auth(client):
    resp = await client.get("/v1/watchlist")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_is_watching_helper(db_session, created_user):
    card = await make_card(db_session)

    assert not await watchlist_service.is_watching(db_session, created_user, card.id)

    await watchlist_service.add(db_session, created_user, card.id)
    assert await watchlist_service.is_watching(db_session, created_user, card.id)

    await watchlist_service.remove(db_session, created_user, card.id)
    assert not await watchlist_service.is_watching(db_session, created_user, card.id)
