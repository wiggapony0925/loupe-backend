"""Unsubscribe tokens, the public one-click endpoint, and recipient filtering."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.user import User, UserSettings
from app.services.auth import unsubscribe_service, user_service
from tests.factories import make_user


def test_token_roundtrip():
    uid = str(uuid.uuid4())
    token = unsubscribe_service.mint_token(uid)
    assert unsubscribe_service.resolve_token(token) == uuid.UUID(uid)


def test_tampered_and_garbage_tokens_fail():
    uid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    good = unsubscribe_service.mint_token(uid)
    _, _, sig = good.partition(".")
    assert unsubscribe_service.resolve_token(f"{other}.{sig}") is None
    assert unsubscribe_service.resolve_token("garbage") is None
    assert unsubscribe_service.resolve_token("") is None
    assert unsubscribe_service.resolve_token(f"{uid}.") is None


def test_unsubscribe_url_points_at_the_public_endpoint():
    url = unsubscribe_service.unsubscribe_url(str(uuid.uuid4()))
    assert "/v1/public/unsubscribe?token=" in url


@pytest.mark.asyncio
async def test_one_click_unsubscribe_flow(client, db_session):
    user = await make_user(db_session)
    token = unsubscribe_service.mint_token(str(user.id))

    # GET (footer link) — flips the preference and confirms in HTML.
    resp = await client.get(f"/v1/public/unsubscribe?token={token}")
    assert resp.status_code == 200
    assert "unsubscribed" in resp.text.lower()

    row = (
        await db_session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
    ).scalar_one()
    assert row.email_announcements_enabled is False

    # POST (RFC 8058 one-click from the mail provider) — idempotent.
    resp = await client.post(f"/v1/public/unsubscribe?token={token}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_invalid_token_is_rejected(client):
    resp = await client.get("/v1/public/unsubscribe?token=not-a-real-token")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_announcement_recipients_honor_the_opt_out(db_session):
    subscribed = await make_user(db_session)
    unsubscribed = await make_user(db_session)
    no_settings_row = User(email=f"bare+{uuid.uuid4().hex[:6]}@t.co")
    banned = await make_user(db_session)

    from datetime import UTC, datetime

    banned.banned_at = datetime.now(UTC)
    db_session.add(no_settings_row)
    # make_user seeds a settings row (subscribed by default) — flip one off.
    opt_out = (
        await db_session.execute(
            select(UserSettings).where(UserSettings.user_id == unsubscribed.id)
        )
    ).scalar_one()
    opt_out.email_announcements_enabled = False
    await db_session.commit()

    recipients = await user_service.announcement_recipients(db_session)
    emails = {email for _uid, email in recipients}
    assert subscribed.email in emails
    assert no_settings_row.email in emails  # defaults to subscribed
    assert unsubscribed.email not in emails
    assert banned.email not in emails
