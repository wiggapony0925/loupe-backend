"""Change-password flow: verify current, set new, revoke other sessions."""

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _register(client, email: str, password: str = "Password123"):
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "display_name": "T"},
    )
    return assert_envelope_ok(resp, expected_status=201)


@pytest.mark.asyncio
async def test_change_password_revokes_old_sessions_and_keeps_current(client):
    body = await _register(client, "changepw@example.com")
    old_access = body["access_token"]
    old_headers = {"Authorization": f"Bearer {old_access}"}

    # Old token works before the change.
    assert (await client.get("/v1/me", headers=old_headers)).status_code == 200

    resp = await client.post(
        "/v1/auth/change-password",
        headers=old_headers,
        json={"current_password": "Password123", "new_password": "BrandNew456"},
    )
    pair = assert_envelope_ok(resp)
    new_headers = {"Authorization": f"Bearer {pair['access_token']}"}

    # The freshly-issued token authenticates...
    assert (await client.get("/v1/me", headers=new_headers)).status_code == 200
    # ...but the pre-change token is now revoked (token epoch bumped).
    assert (await client.get("/v1/me", headers=old_headers)).status_code == 401


@pytest.mark.asyncio
async def test_change_password_updates_credentials(client):
    await _register(client, "newcreds@example.com", password="Password123")
    login = await client.post(
        "/v1/auth/login",
        json={"email": "newcreds@example.com", "password": "Password123"},
    )
    headers = {"Authorization": f"Bearer {assert_envelope_ok(login)['access_token']}"}

    await client.post(
        "/v1/auth/change-password",
        headers=headers,
        json={"current_password": "Password123", "new_password": "BrandNew456"},
    )

    # Old password no longer works; the new one does.
    old = await client.post(
        "/v1/auth/login",
        json={"email": "newcreds@example.com", "password": "Password123"},
    )
    assert_envelope_error(old, expected_status=401)
    new = await client.post(
        "/v1/auth/login",
        json={"email": "newcreds@example.com", "password": "BrandNew456"},
    )
    assert_envelope_ok(new)


@pytest.mark.asyncio
async def test_change_password_wrong_current_rejected(client):
    body = await _register(client, "wrongcur@example.com")
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await client.post(
        "/v1/auth/change-password",
        headers=headers,
        json={"current_password": "not-my-password", "new_password": "BrandNew456"},
    )
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_change_password_rejects_short_new_password(client):
    body = await _register(client, "shortpw@example.com")
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await client.post(
        "/v1/auth/change-password",
        headers=headers,
        json={"current_password": "Password123", "new_password": "short"},
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_change_password_unavailable_for_sso_only_account(
    client, created_user, db_session
):
    """`created_user` is an Apple account with no password_hash → 409."""
    from app.auth.jwt import issue_token

    token, _ = issue_token(
        created_user.id, "access", {"ver": created_user.token_version}
    )
    resp = await client.post(
        "/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "whatever1", "new_password": "BrandNew456"},
    )
    assert_envelope_error(resp, expected_status=409)


@pytest.mark.asyncio
async def test_change_password_requires_auth(client):
    resp = await client.post(
        "/v1/auth/change-password",
        json={"current_password": "Password123", "new_password": "BrandNew456"},
    )
    assert_envelope_error(resp, expected_status=401)
