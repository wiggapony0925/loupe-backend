"""Users router tests (profile + settings)."""

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client):
    resp = await client.get("/v1/me")
    assert resp.status_code in (401, 403)
    assert_envelope_error(resp, expected_status=resp.status_code)


@pytest.mark.asyncio
async def test_get_me(client, created_user, auth_headers):
    resp = await client.get("/v1/me", headers=auth_headers)
    body = assert_envelope_ok(resp)
    assert body["email"] == created_user.email


@pytest.mark.asyncio
async def test_patch_me(client, auth_headers):
    resp = await client.patch(
        "/v1/me", headers=auth_headers, json={"display_name": "Renamed"}
    )
    body = assert_envelope_ok(resp)
    assert body["display_name"] == "Renamed"


@pytest.mark.asyncio
async def test_get_settings_creates_defaults(client, auth_headers):
    resp = await client.get("/v1/me/settings", headers=auth_headers)
    body = assert_envelope_ok(resp)
    assert "currency" in body
    assert "theme" in body


@pytest.mark.asyncio
async def test_patch_settings(client, auth_headers):
    resp = await client.patch(
        "/v1/me/settings", headers=auth_headers, json={"currency": "EUR"}
    )
    body = assert_envelope_ok(resp)
    assert body["currency"] == "EUR"


@pytest.mark.asyncio
async def test_active_collection_roundtrip(client, auth_headers):
    # Defaults to the "All" view (null).
    body = assert_envelope_ok(await client.get("/v1/me/settings", headers=auth_headers))
    assert body["active_collection_id"] is None

    # Set a specific portfolio — persists so mobile + web follow.
    body = assert_envelope_ok(
        await client.patch(
            "/v1/me/settings",
            headers=auth_headers,
            json={"active_collection_id": "col_umbreon"},
        )
    )
    assert body["active_collection_id"] == "col_umbreon"

    # Explicit null clears back to "All"; omitting the field leaves it unchanged.
    body = assert_envelope_ok(
        await client.patch(
            "/v1/me/settings", headers=auth_headers, json={"currency": "USD"}
        )
    )
    assert body["active_collection_id"] == "col_umbreon"  # untouched
    body = assert_envelope_ok(
        await client.patch(
            "/v1/me/settings",
            headers=auth_headers,
            json={"active_collection_id": None},
        )
    )
    assert body["active_collection_id"] is None
