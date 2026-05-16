"""Users router tests (profile + settings)."""

import pytest


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client):
    resp = await client.get("/v1/me")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_me(client, created_user, auth_headers):
    resp = await client.get("/v1/me", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == created_user.email


@pytest.mark.asyncio
async def test_patch_me(client, auth_headers):
    resp = await client.patch(
        "/v1/me", headers=auth_headers, json={"display_name": "Renamed"}
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"


@pytest.mark.asyncio
async def test_get_settings_creates_defaults(client, auth_headers):
    resp = await client.get("/v1/me/settings", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "currency" in body
    assert "theme" in body


@pytest.mark.asyncio
async def test_patch_settings(client, auth_headers):
    resp = await client.patch(
        "/v1/me/settings", headers=auth_headers, json={"currency": "EUR"}
    )
    assert resp.status_code == 200
    assert resp.json()["currency"] == "EUR"
