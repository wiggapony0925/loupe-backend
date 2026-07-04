"""Email verification — tokens, the public landing, and signup wiring."""

from __future__ import annotations

import uuid

import pytest

from app.services import email_service
from app.services.auth import email_verify_service, user_service
from tests.factories import make_user


class _Recorder:
    def __init__(self):
        self.calls: list = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


def test_verify_token_roundtrip_and_tamper():
    uid = str(uuid.uuid4())
    token = email_verify_service.mint_token(uid)
    assert email_verify_service.resolve_token(token) == uuid.UUID(uid)
    assert (
        email_verify_service.resolve_token(f"{uuid.uuid4()}.{token.split('.')[1]}")
        is None
    )
    assert email_verify_service.resolve_token("junk") is None


@pytest.mark.asyncio
async def test_public_verify_endpoint_flips_the_flag(client, db_session):
    user = await make_user(db_session)
    assert user.email_verified_at is None  # factory users start unverified

    token = email_verify_service.mint_token(str(user.id))
    resp = await client.get(f"/v1/public/verify-email?token={token}")
    assert resp.status_code == 200
    assert "confirmed" in resp.text.lower()

    await db_session.refresh(user)
    assert user.email_verified_at is not None
    assert user.email_verified is True

    # Replay is harmless; garbage is a 400 page.
    resp = await client.get(f"/v1/public/verify-email?token={token}")
    assert resp.status_code == 200
    resp = await client.get("/v1/public/verify-email?token=nope-nope-nope")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_register_welcome_carries_a_verify_link(client, monkeypatch):
    welcome = _Recorder()
    monkeypatch.setattr("app.routers.auth.auth.email_service.send_welcome", welcome)
    resp = await client.post(
        "/v1/auth/register",
        json={"email": f"v+{uuid.uuid4().hex[:8]}@t.co", "password": "password-11"},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["user"]["email_verified"] is False
    assert len(welcome.calls) == 1
    assert "/v1/public/verify-email?token=" in welcome.calls[0][1]["verify_url"]


@pytest.mark.asyncio
async def test_social_accounts_arrive_verified(db_session):
    user = await user_service.find_or_create_by_apple(
        db_session,
        apple_sub=f"apple-{uuid.uuid4().hex}",
        email=f"sv+{uuid.uuid4().hex[:8]}@t.co",
        display_name="Verified",
    )
    assert user.email_verified_at is not None


@pytest.mark.asyncio
async def test_resend_endpoint(client, created_user, auth_headers, monkeypatch):
    resend = _Recorder()
    monkeypatch.setattr(email_service, "send_verify_email", resend)

    resp = await client.post("/v1/me/verify-email/resend", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["sent"] is True
    assert len(resend.calls) == 1

    # Once verified, resending politely no-ops.
    token = email_verify_service.mint_token(str(created_user.id))
    await client.get(f"/v1/public/verify-email?token={token}")
    resp = await client.post("/v1/me/verify-email/resend", headers=auth_headers)
    assert resp.json()["data"]["already_verified"] is True
    assert len(resend.calls) == 1
