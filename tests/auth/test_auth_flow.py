"""Apple/Google sign-in + refresh endpoint tests (with monkeypatched verifiers)."""

import pytest

from app.auth.apple import AppleClaims
from app.auth.google import GoogleClaims
from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.mark.asyncio
async def test_apple_sign_in_creates_user(client, monkeypatch):
    async def fake_verify(_token: str) -> AppleClaims:
        return AppleClaims(
            sub="apple-sub-1", email="alice@example.com", email_verified=True
        )

    monkeypatch.setattr(
        "app.routers.auth.auth.verify_apple_identity_token", fake_verify
    )

    resp = await client.post(
        "/v1/auth/apple",
        json={"identity_token": "fake-token-1234567890", "display_name": "Alice"},
    )
    body = assert_envelope_ok(resp)
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

    monkeypatch.setattr("app.routers.auth.auth.verify_google_id_token", fake_verify)

    resp = await client.post(
        "/v1/auth/google", json={"id_token": "fake-token-1234567890"}
    )
    body = assert_envelope_ok(resp)
    assert body["user"]["email"] == "bob@example.com"


@pytest.mark.asyncio
async def test_refresh_token_rotation(client, monkeypatch):
    async def fake_verify(_token: str) -> AppleClaims:
        return AppleClaims(
            sub="apple-refresh-x", email="x@example.com", email_verified=True
        )

    monkeypatch.setattr(
        "app.routers.auth.auth.verify_apple_identity_token", fake_verify
    )

    first = await client.post(
        "/v1/auth/apple", json={"identity_token": "fake-token-1234567890"}
    )
    refresh = assert_envelope_ok(first)["refresh_token"]

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    body = assert_envelope_ok(resp)
    assert body["access_token"]


@pytest.mark.asyncio
async def test_refresh_rejects_invalid(client):
    resp = await client.post("/v1/auth/refresh", json={"refresh_token": "a" * 50})
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_refresh_rejects_banned_user(client, created_user, db_session):
    """A banned user must not be able to mint fresh access tokens via refresh."""
    from datetime import UTC, datetime

    from app.auth.jwt import issue_token

    refresh, _ = issue_token(created_user.id, "refresh")
    created_user.banned_at = datetime.now(UTC)
    db_session.add(created_user)
    await db_session.commit()

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert_envelope_error(resp, expected_status=403)


@pytest.mark.asyncio
async def test_logout_all_revokes_outstanding_tokens(client, created_user):
    """`/auth/logout-all` bumps the token epoch so a previously-valid token stops
    authenticating — the 'sign out everywhere' / stolen-token kill switch."""
    from app.auth.jwt import issue_token

    token, _ = issue_token(
        created_user.id, "access", {"ver": created_user.token_version}
    )
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/v1/me", headers=headers)).status_code == 200

    revoke = await client.post("/v1/auth/logout-all", headers=headers)
    assert revoke.status_code == 204

    # The same token no longer works.
    assert (await client.get("/v1/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_access_token_with_stale_ver_rejected(client, created_user, db_session):
    from app.auth.jwt import issue_token

    token, _ = issue_token(created_user.id, "access", {"ver": 0})
    created_user.token_version = 5  # epoch advanced out from under the token
    db_session.add(created_user)
    await db_session.commit()

    resp = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_stale_ver_rejected(client, created_user, db_session):
    from app.auth.jwt import issue_token

    refresh, _ = issue_token(created_user.id, "refresh", {"ver": 0})
    created_user.token_version = 1
    db_session.add(created_user)
    await db_session.commit()

    resp = await client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_legacy_token_without_ver_claim_still_valid(client, auth_headers):
    """Tokens minted before this feature carry no `ver` claim; they must still
    validate (the check defaults a missing claim to epoch 0)."""
    resp = await client.get("/v1/me", headers=auth_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_after_logout_all_mints_a_valid_token(client):
    """Regression: every sign-in path must stamp the *current* token epoch, so a
    fresh login after a revocation isn't born already-stale."""
    creds = {"email": "revoke-relog@example.com", "password": "Password123"}

    first = await client.post("/v1/auth/register", json={**creds, "display_name": "R"})
    access = assert_envelope_ok(first, expected_status=201)["access_token"]

    # Revoke everything, then sign back in with email/password.
    revoke = await client.post(
        "/v1/auth/logout-all", headers={"Authorization": f"Bearer {access}"}
    )
    assert revoke.status_code == 204

    relog = await client.post("/v1/auth/login", json=creds)
    new_access = assert_envelope_ok(relog)["access_token"]

    # The newly-minted token must authenticate (carries the bumped epoch).
    me = await client.get("/v1/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200
