"""Background dispatch (build-now, send-later) and the support send path."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import email_service
from tests.email.test_email_service import _FakeClient
from tests.factories import make_user


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
    email = "bg@example.com"
    display_name = "BG"


# ── Dispatch ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_wrappers_deliver_in_the_background(provider):
    provider.script = [200]
    queued = await email_service.send_welcome(_StubUser())
    assert queued is True
    # Nothing on the wire yet — the API call would have returned already.
    # (The task may not have been scheduled a slot on the loop.)
    await email_service.drain()
    assert len(provider.calls) == 1
    assert provider.calls[0]["json"]["to"] == ["bg@example.com"]
    assert provider.calls[0]["json"]["subject"] == "Welcome to Loupe"


@pytest.mark.asyncio
async def test_queue_is_a_noop_without_a_provider():
    assert get_settings().email_enabled is False
    queued = await email_service.send_welcome(_StubUser())
    assert queued is False
    await email_service.drain()  # nothing pending, returns immediately


@pytest.mark.asyncio
async def test_background_send_carries_idempotency_key(provider):
    provider.script = [200]
    await email_service.send_pro_activated(_StubUser(), idempotency_key="pro-x-1")
    await email_service.drain()
    assert provider.calls[0]["headers"]["Idempotency-Key"] == "pro-x-1"


# ── Support template ──────────────────────────────────────────────────────


def test_support_message_has_no_unsubscribe_footer():
    c = email_service.build_support_message(
        recipient_name="Sam",
        subject="About your account",
        body_text="Quick note about <your> vault.",
        cta=("Open Loupe", "https://loupe.app"),
    )
    assert "Unsubscribe" not in c.html
    assert "Hi Sam," in c.html
    assert "&lt;your&gt;" in c.html  # body escaped
    assert "reply to this email" in c.text


@pytest.mark.asyncio
async def test_send_support_message_uses_the_support_sender(provider, monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "support_from_email",
        "Loupe Support <support@t.co>",
        raising=False,
    )
    provider.script = [200]
    ok = await email_service.send_support_message(
        _StubUser(), subject="Hello", body_text="Hi."
    )
    assert ok is True  # synchronous — the admin sees the provider verdict
    assert provider.calls[0]["json"]["from"] == "Loupe Support <support@t.co>"
    assert provider.calls[0]["json"]["tags"] == [
        {"name": "category", "value": "support"}
    ]


# ── Admin endpoints ───────────────────────────────────────────────────────


def _as_admin(settings, email):
    prev = settings.admin_emails
    settings.admin_emails = email
    return prev


@pytest.mark.asyncio
async def test_support_preview_kind_renders_without_footer(
    client, created_user, auth_headers
):
    settings = get_settings()
    prev = _as_admin(settings, created_user.email)
    try:
        resp = await client.post(
            "/v1/admin/email/announce/preview",
            headers=auth_headers,
            json={"subject": "s", "body": "Hello there.", "kind": "support"},
        )
        assert resp.status_code == 200
        html = resp.json()["data"]["html"]
        assert "Unsubscribe" not in html
        assert "Hi there," in html
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_support_send_endpoint(
    client, created_user, auth_headers, db_session, monkeypatch
):
    settings = get_settings()
    prev = _as_admin(settings, created_user.email)
    monkeypatch.setattr(settings, "resend_api_key", "re_test", raising=False)
    monkeypatch.setattr(
        settings, "notifications_from_email", "Loupe <t@t.co>", raising=False
    )

    class _SendRecorder:
        calls: list = []

        @staticmethod
        async def record(*args, **kwargs):
            _SendRecorder.calls.append((args, kwargs))
            return True

    monkeypatch.setattr(email_service, "send_support_message", _SendRecorder.record)
    target = await make_user(db_session)
    try:
        # Unknown account → 404, nothing sent.
        resp = await client.post(
            "/v1/admin/email/support",
            headers=auth_headers,
            json={
                "email": "ghost@nowhere.dev",
                "subject": "s",
                "body": "b",
                "mode": "send",
            },
        )
        assert resp.status_code == 404

        # Real account → sent, audit-visible target.
        resp = await client.post(
            "/v1/admin/email/support",
            headers=auth_headers,
            json={
                "email": target.email,
                "subject": "About your vault",
                "body": "One of your cards was re-priced.",
                "mode": "send",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sent"] is True
        assert data["to"] == target.email
        assert len(_SendRecorder.calls) == 1
        assert _SendRecorder.calls[0][1]["subject"] == "About your vault"
    finally:
        settings.admin_emails = prev  # type: ignore[misc]
