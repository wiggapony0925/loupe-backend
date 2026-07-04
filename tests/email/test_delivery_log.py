"""The delivery log: pipeline writes, webhook updates, suppression, retry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.email_log import EmailLog
from app.models.user import UserSettings
from app.services import email_service
from tests.factories import make_user

_TEST_WEBHOOK_SECRET = (
    "whsec_" + base64.b64encode(b"test-secret-32-bytes-long!!").decode()
)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.text = f"status {status_code}"

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeClient:
    calls: list[dict] = []
    script: list[_FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeClient.script.pop(0)


@pytest.fixture
def provider(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", "re_test", raising=False)
    monkeypatch.setattr(
        settings, "notifications_from_email", "Loupe <t@t.co>", raising=False
    )
    monkeypatch.setattr(email_service.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(email_service, "_RETRY_DELAY_SEC", 0)
    _FakeClient.calls = []
    _FakeClient.script = []
    return _FakeClient


class _StubUser:
    email = "logged@example.com"
    display_name = "Logged"


async def _one_row(db_session, to_email: str) -> EmailLog:
    return (
        await db_session.execute(select(EmailLog).where(EmailLog.to_email == to_email))
    ).scalar_one()


# ── Pipeline writes ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_queued_send_lands_in_the_log_with_provider_id(
    db_session, db_engine, provider
):
    provider.script = [_FakeResponse(200, {"id": "re_msg_1"})]
    await email_service.send_welcome(_StubUser())
    await email_service.drain()

    row = await _one_row(db_session, "logged@example.com")
    assert row.status == "sent"
    assert row.provider_id == "re_msg_1"
    assert row.category == "welcome"
    assert row.subject == "Welcome to Loupe"
    assert row.html and row.html.startswith("<!DOCTYPE html>")


@pytest.mark.asyncio
async def test_failed_send_records_error_and_attempts(db_session, db_engine, provider):
    provider.script = [_FakeResponse(422)]
    await email_service.send_welcome(_StubUser())
    await email_service.drain()

    row = await _one_row(db_session, "logged@example.com")
    assert row.status == "failed"
    assert "422" in (row.error or "")
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_batch_send_logs_each_recipient_with_ids(db_session, db_engine, provider):
    provider.script = [_FakeResponse(200, {"data": [{"id": "re_b1"}, {"id": "re_b2"}]})]
    sent = await email_service.send_blog_announcement(
        [("a1@t.co", "https://u.test/1"), ("a2@t.co", "https://u.test/2")],
        title="Post",
        excerpt="",
        slug="post",
    )
    assert sent == 2
    row1 = await _one_row(db_session, "a1@t.co")
    row2 = await _one_row(db_session, "a2@t.co")
    assert (row1.status, row1.provider_id) == ("sent", "re_b1")
    assert (row2.status, row2.provider_id) == ("sent", "re_b2")
    assert row1.category == "announcement"


# ── Webhook ───────────────────────────────────────────────────────────────


def _signed_headers(body: bytes, *, secret: str = _TEST_WEBHOOK_SECRET) -> dict:
    msg_id = "msg_test"
    ts = str(int(time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(
        hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}


@pytest.fixture
def webhook_secret(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "resend_webhook_secret", _TEST_WEBHOOK_SECRET, raising=False
    )


@pytest.mark.asyncio
async def test_webhook_rejects_unsigned_and_unconfigured(client, monkeypatch):
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "x"}}).encode()
    # Unconfigured → 503.
    resp = await client.post("/v1/webhooks/resend", content=body)
    assert resp.status_code == 503
    # Configured but bad signature → 401.
    monkeypatch.setattr(
        get_settings(), "resend_webhook_secret", _TEST_WEBHOOK_SECRET, raising=False
    )
    resp = await client.post(
        "/v1/webhooks/resend",
        content=body,
        headers={
            "svix-id": "m",
            "svix-timestamp": str(int(time.time())),
            "svix-signature": "v1,bogus",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webhook_advances_status_and_suppresses_bounces(
    client, db_session, db_engine, provider, webhook_secret
):
    user = await make_user(db_session)

    class _U:
        email = user.email
        display_name = "U"
        id = user.id

    provider.script = [_FakeResponse(200, {"id": "re_hook_1"})]
    await email_service.send_welcome(_U())
    await email_service.drain()

    # delivered advances the row.
    body = json.dumps(
        {"type": "email.delivered", "data": {"email_id": "re_hook_1"}}
    ).encode()
    resp = await client.post(
        "/v1/webhooks/resend", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 200
    row = await _one_row(db_session, user.email)
    await db_session.refresh(row)
    assert row.status == "delivered"

    # bounce advances further AND suppresses announcements for the address.
    body = json.dumps(
        {"type": "email.bounced", "data": {"email_id": "re_hook_1"}}
    ).encode()
    resp = await client.post(
        "/v1/webhooks/resend", content=body, headers=_signed_headers(body)
    )
    assert resp.status_code == 200
    await db_session.refresh(row)
    assert row.status == "bounced"
    settings_row = (
        await db_session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
    ).scalar_one()
    await db_session.refresh(settings_row)
    assert settings_row.email_announcements_enabled is False


# ── Admin log endpoints ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_log_list_detail_and_retry(
    client, created_user, auth_headers, db_session, db_engine, provider
):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = created_user.email  # type: ignore[misc]
    try:
        # One failed send to work with.
        provider.script = [_FakeResponse(500), _FakeResponse(500)]  # retry exhausts
        await email_service.send_welcome(_StubUser())
        await email_service.drain()

        resp = await client.get("/v1/admin/email/log", headers=auth_headers)
        assert resp.status_code == 200
        page = resp.json()["data"]
        assert page["total"] == 1
        assert page["stats"]["failed"] == 1
        entry = page["rows"][0]
        assert entry["to_email"] == "logged@example.com"
        assert entry["status"] == "failed"

        # Detail carries the stored render.
        resp = await client.get(
            f"/v1/admin/email/log/{entry['id']}", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["html"].startswith("<!DOCTYPE html>")

        # Retry re-sends the stored content and flips the row to sent.
        provider.script = [_FakeResponse(200, {"id": "re_retry_1"})]
        resp = await client.post(
            f"/v1/admin/email/log/{entry['id']}/retry", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["sent"] is True
        row = await db_session.get(EmailLog, uuid.UUID(entry["id"]))
        await db_session.refresh(row)
        assert row.status == "sent"
        assert row.provider_id == "re_retry_1"

        # A sent row can't be retried again.
        resp = await client.post(
            f"/v1/admin/email/log/{entry['id']}/retry", headers=auth_headers
        )
        assert resp.status_code == 409
    finally:
        settings.admin_emails = prev  # type: ignore[misc]
