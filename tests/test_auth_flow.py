"""Apple/Google sign-in + refresh endpoint tests (with monkeypatched verifiers)."""

import pytest

from app.auth.apple import AppleClaims
from app.auth.google import GoogleClaims


@pytest.mark.asyncio
async def test_apple_sign_in_creates_user(client, monkeypatch):
    async def fake_verify(_token: str) -> AppleClaims:
        return AppleClaims(
            sub="apple-sub-1", email="alice@example.com", email_verified=True
        )

    monkeypatch.setattr("app.routers.auth.verify_apple_identity_token", fake_verify)

    resp = await client.post(
        "/v1/auth/apple",
        json={"identity_token": "fake-token-1234567890", "display_name": "Alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "alice@example.com"
    assert body["access_token"]
    assert body["refresh_token"]


@pytest.mark.asyncio
async def test_google_sign_in_creates_user(client, monkeypatch):
    async def fake_verify(_token: str) -> GoogleClaims:
        return GoogleClaims(
            sub="g-sub-1",
            email="bob@example.com",
            email_verified=True,
            name="Bob",
            picture=None,
        )

    monkeypatch.setattr("app.routers.auth.verify_google_id_token", fake_verify)

    resp = await client.post(
        "/v1/auth/google", json={"id_token": "fake-token-1234567890"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_refresh_token_rotation(client, monkeypatch):
    async def fake_verify(_token: str) -> AppleClaims:
        return AppleClaims(
            sub="apple-refresh-x", email="x@example.com", email_verified=True
        )

    monkeypatch.setattr("app.routers.auth.verify_apple_identity_token", fake_verify)

    first = await client.post(
        "/v1/auth/apple", json={"identity_token": "fake-token-1234567890"}
    )
    refresh = first.json()["refresh_token"]

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


@pytest.mark.asyncio
async def test_refresh_rejects_invalid(client):
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "a" * 50})
    assert resp.status_code == 401
