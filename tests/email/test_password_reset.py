"""Forgot/reset password — token semantics and the public endpoints."""

from __future__ import annotations

import time
import uuid

import pytest

from app.auth.passwords import verify_password
from app.services import email_service
from app.services.auth import password_reset_service, user_service


class _Recorder:
    def __init__(self):
        self.calls: list = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


# ── Token semantics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_token_roundtrip_and_single_use(db_session):
    user = await user_service.create_with_password(
        db_session, email=f"r+{uuid.uuid4().hex[:8]}@t.co", password="old-password-1"
    )
    token = password_reset_service.mint_token(user)

    updated = await password_reset_service.perform_reset(
        db_session, token, "new-password-2"
    )
    assert verify_password("new-password-2", updated.password_hash)

    # Same token again: dead — the reset bumped token_version and changed the
    # password hash, both of which the signature covers.
    with pytest.raises(password_reset_service.ResetTokenError):
        await password_reset_service.perform_reset(db_session, token, "another-3")


@pytest.mark.asyncio
async def test_expired_and_tampered_tokens_fail(db_session, monkeypatch):
    user = await user_service.create_with_password(
        db_session, email=f"r+{uuid.uuid4().hex[:8]}@t.co", password="old-password-1"
    )
    # Expired: mint with a clock 31 minutes in the past.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 31 * 60)
    stale = password_reset_service.mint_token(user)
    monkeypatch.setattr(time, "time", real_time)
    with pytest.raises(password_reset_service.ResetTokenError):
        await password_reset_service.perform_reset(db_session, stale, "whatever-9")

    # Tampered: valid shape, wrong signature.
    uid, exp, _sig = password_reset_service.mint_token(user).split(".", 2)
    with pytest.raises(password_reset_service.ResetTokenError):
        await password_reset_service.perform_reset(
            db_session, f"{uid}.{exp}.{'0' * 32}", "whatever-9"
        )

    # Garbage.
    with pytest.raises(password_reset_service.ResetTokenError):
        await password_reset_service.perform_reset(db_session, "garbage", "whatever-9")


@pytest.mark.asyncio
async def test_request_reset_routes_by_account_type(db_session, monkeypatch):
    reset = _Recorder()
    unavailable = _Recorder()
    monkeypatch.setattr(email_service, "send_password_reset", reset)
    monkeypatch.setattr(email_service, "send_reset_unavailable", unavailable)

    # Password account → reset link.
    pw_user = await user_service.create_with_password(
        db_session, email=f"pw+{uuid.uuid4().hex[:8]}@t.co", password="password-88"
    )
    await password_reset_service.request_reset(db_session, pw_user.email)
    assert len(reset.calls) == 1
    assert "/reset-password?token=" in reset.calls[0][0][1]

    # Social-only account → "you sign in with Apple/Google".
    social = await user_service.find_or_create_by_apple(
        db_session,
        apple_sub=f"apple-{uuid.uuid4().hex}",
        email=f"so+{uuid.uuid4().hex[:8]}@t.co",
        display_name="Social",
    )
    await password_reset_service.request_reset(db_session, social.email)
    assert len(unavailable.calls) == 1

    # Unknown email → silence, no exception.
    await password_reset_service.request_reset(db_session, "nobody@nowhere.test")
    assert len(reset.calls) == 1
    assert len(unavailable.calls) == 1


# ── Endpoints ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forgot_password_endpoint_never_reveals_accounts(client):
    resp = await client.post(
        "/v1/auth/forgot-password", json={"email": "ghost@example.com"}
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_reset_password_endpoint_end_to_end(client, db_session, monkeypatch):
    sent: list[str] = []

    async def capture(user, reset_url):
        sent.append(reset_url)
        return True

    monkeypatch.setattr(email_service, "send_password_reset", capture)

    email = f"e2e+{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "first-password-1"},
    )
    assert resp.status_code == 201

    resp = await client.post("/v1/auth/forgot-password", json={"email": email})
    assert resp.status_code == 204
    assert len(sent) == 1
    token = sent[0].split("token=", 1)[1]

    # Bad token → 400 with a friendly message.
    resp = await client.post(
        "/v1/auth/reset-password",
        json={"token": "not-a-token-at-all", "new_password": "second-password-2"},
    )
    assert resp.status_code == 400

    # Real token → signed in with a fresh pair; old password dead, new works.
    resp = await client.post(
        "/v1/auth/reset-password",
        json={"token": token, "new_password": "second-password-2"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["access_token"]

    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "first-password-1"}
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "second-password-2"}
    )
    assert resp.status_code == 200
