"""The moments that send email — transition-only semantics and admin surface."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models.enums import WaitlistStatusEnum
from app.models.waitlist import WaitlistEntry
from app.schemas.waitlist import WaitlistStatusUpdate
from app.services import billing_service, email_service
from app.services.portal import waitlist_service
from tests.factories import make_user


class _Recorder:
    def __init__(self):
        self.calls: list = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


# ── Waitlist invite (admin advances to `invited`) ─────────────────────────


@pytest.mark.asyncio
async def test_waitlist_invite_sends_only_on_the_transition(db_session, monkeypatch):
    invite = _Recorder()
    monkeypatch.setattr(email_service, "send_waitlist_invite", invite)

    entry = WaitlistEntry(email="fan@example.com", name="Sam", status="waiting")
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    await waitlist_service.admin_update_status(
        db_session, entry.id, WaitlistStatusUpdate(status=WaitlistStatusEnum.invited)
    )
    assert len(invite.calls) == 1
    assert invite.calls[0][0][0] == "fan@example.com"

    # Re-saving `invited` stays silent.
    await waitlist_service.admin_update_status(
        db_session, entry.id, WaitlistStatusUpdate(status=WaitlistStatusEnum.invited)
    )
    assert len(invite.calls) == 1

    # Moving on to `purchased` doesn't re-invite.
    await waitlist_service.admin_update_status(
        db_session, entry.id, WaitlistStatusUpdate(status=WaitlistStatusEnum.purchased)
    )
    assert len(invite.calls) == 1


# ── Billing (Stripe webhook plan sync) ────────────────────────────────────


@pytest.mark.asyncio
async def test_pro_emails_fire_on_transitions_not_renewals(db_session, monkeypatch):
    activated = _Recorder()
    canceled = _Recorder()
    monkeypatch.setattr(email_service, "send_pro_activated", activated)
    monkeypatch.setattr(email_service, "send_pro_canceled", canceled)

    user = await make_user(db_session)
    user.stripe_customer_id = "cus_test1"
    await db_session.commit()

    sub = {"id": "sub_1", "customer": "cus_test1", "status": "active"}

    # free → pro: one activation email.
    await billing_service._apply_subscription(db_session, sub)
    assert len(activated.calls) == 1
    assert activated.calls[0][1]["idempotency_key"] == "pro-activated-sub_1"

    # Renewal (still active): silence.
    await billing_service._apply_subscription(db_session, sub)
    assert len(activated.calls) == 1
    assert len(canceled.calls) == 0

    # pro → free: one cancellation email.
    await billing_service._apply_subscription(
        db_session, {"id": "sub_1", "customer": "cus_test1", "status": "canceled"}
    )
    assert len(canceled.calls) == 1
    assert len(activated.calls) == 1


# ── Security notices (auth endpoints) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_password_change_sends_a_security_notice(client, monkeypatch):
    notice = _Recorder()
    monkeypatch.setattr(
        "app.routers.auth.auth.email_service.send_password_changed", notice
    )

    email = f"sec+{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-9", "display_name": "Sec"},
    )
    assert resp.status_code == 201
    token = resp.json()["data"]["access_token"]

    resp = await client.post(
        "/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "correct-horse-9",
            "new_password": "battery-staple-10",
        },
    )
    assert resp.status_code == 200
    assert len(notice.calls) == 1

    # A rejected attempt (wrong current password) must NOT notify.
    token2 = resp.json()["data"]["access_token"]
    resp = await client.post(
        "/v1/auth/change-password",
        headers={"Authorization": f"Bearer {token2}"},
        json={"current_password": "wrong", "new_password": "battery-staple-11"},
    )
    assert resp.status_code == 401
    assert len(notice.calls) == 1


# ── Admin email surface ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_email_gallery_lists_and_renders(
    client, created_user, auth_headers
):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = created_user.email  # type: ignore[misc]
    try:
        resp = await client.get("/v1/admin/email/templates", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"]["enabled"] is False  # no provider in tests
        keys = {t["key"] for t in data["templates"]}
        assert {"welcome", "price_alert", "blog_announcement"} <= keys

        resp = await client.get(
            "/v1/admin/email/templates/welcome", headers=auth_headers
        )
        assert resp.status_code == 200
        render = resp.json()["data"]
        assert render["html"].startswith("<!DOCTYPE html>")
        assert render["text"].strip()

        resp = await client.get("/v1/admin/email/templates/nope", headers=auth_headers)
        assert resp.status_code == 404

        # Test send without a provider: explicit "not configured", not an error.
        resp = await client.post(
            "/v1/admin/email/test", headers=auth_headers, json={"template": "welcome"}
        )
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["sent"] is False
        assert "not configured" in body["detail"].lower()
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_admin_email_surface_requires_admin(client, auth_headers):
    resp = await client.get("/v1/admin/email/templates", headers=auth_headers)
    assert resp.status_code == 403
    resp = await client.get("/v1/admin/email/templates")
    assert resp.status_code in (401, 403)


# ── Custom announcement composer ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_announcement_preview_escapes_and_renders(
    client, created_user, auth_headers
):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = created_user.email  # type: ignore[misc]
    try:
        resp = await client.post(
            "/v1/admin/email/announce/preview",
            headers=auth_headers,
            json={
                "subject": "Big news",
                "heading": "Hello <script>",
                "body": "First paragraph.\n\nSecond & final.",
                "cta_label": "Open",
                "cta_url": "https://loupe.app/app",
            },
        )
        assert resp.status_code == 200
        render = resp.json()["data"]
        assert "&lt;script&gt;" in render["html"]
        assert "<script>" not in render["html"]
        assert "Second &amp; final." in render["html"]
        assert "Unsubscribe" in render["html"]

        # Non-http CTA links are rejected at validation time.
        resp = await client.post(
            "/v1/admin/email/announce/preview",
            headers=auth_headers,
            json={
                "subject": "s",
                "heading": "h",
                "body": "b",
                "cta_label": "x",
                "cta_url": "javascript:alert(1)",
            },
        )
        assert resp.status_code == 422
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


@pytest.mark.asyncio
async def test_announcement_send_counts_recipients(
    client, created_user, auth_headers, monkeypatch
):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = created_user.email  # type: ignore[misc]
    monkeypatch.setattr(settings, "resend_api_key", "re_test", raising=False)
    monkeypatch.setattr(
        settings, "notifications_from_email", "Loupe <t@t.co>", raising=False
    )
    blast = _Recorder()
    monkeypatch.setattr(email_service, "send_custom_announcement", blast)
    try:
        resp = await client.post(
            "/v1/admin/email/announce",
            headers=auth_headers,
            json={
                "subject": "Ship day",
                "heading": "We shipped",
                "body": "Lots of things.",
                "mode": "send",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["mode"] == "send"
        assert data["recipients"] == 1  # the one active (admin) user
        # Queued as a background task with per-recipient unsubscribe URLs.
        assert len(blast.calls) == 1
        recipients = blast.calls[0][0][0]
        assert recipients[0][0] == created_user.email
        assert "/v1/public/unsubscribe?token=" in recipients[0][1]
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


# ── Statement ready ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_statement_ready_email_fires_after_generation(db_session, monkeypatch):
    from app.models.enums import ReportPeriodEnum
    from app.services.analytics.reports import service as reports_service

    ready = _Recorder()
    monkeypatch.setattr(email_service, "send_statement_ready", ready)

    async def fake_snapshot(*args, **kwargs):
        return {"fake": True}

    async def fake_upload(*args, **kwargs):
        return "reports/fake.pdf"

    monkeypatch.setattr(reports_service, "build_snapshot", fake_snapshot)
    monkeypatch.setattr(reports_service, "render_pdf", lambda snap: b"%PDF-fake")
    monkeypatch.setattr(reports_service, "upload_report_pdf", fake_upload)

    user = await make_user(db_session)
    row = await reports_service.generate_report(
        db_session, user=user, period=ReportPeriodEnum.monthly, year=2026, month=5
    )
    assert row.status.value == "ready"
    assert len(ready.calls) == 1
    assert ready.calls[0][1]["title"] == "May 2026 statement"
    assert ready.calls[0][1]["idempotency_key"] == f"statement-ready-{row.id}"


# ── Brute-force lockout ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lockout_notifies_once_on_the_transition(client, monkeypatch):
    """The owner hears about the lock exactly when it trips — not on every
    subsequent rejected attempt (which would be a mail-bomb vector)."""
    locked = _Recorder()
    monkeypatch.setattr(email_service, "send_account_locked", locked)

    email = f"lock+{uuid.uuid4().hex[:8]}@example.com"
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correct-horse-9", "display_name": "Lock"},
    )
    assert resp.status_code == 201

    attempts = get_settings().login_max_attempts
    for _ in range(attempts):
        await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
    assert len(locked.calls) == 1
    assert locked.calls[0][1]["attempts"] == attempts
    assert locked.calls[0][1]["minutes"] >= 1

    # Further attempts while already locked must stay silent.
    for _ in range(3):
        await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
    assert len(locked.calls) == 1


# ── Dunning (Stripe invoice.payment_failed) ───────────────────────────────


@pytest.mark.asyncio
async def test_payment_failure_notifies_the_subscriber(db_session, monkeypatch):
    failed = _Recorder()
    monkeypatch.setattr(email_service, "send_payment_failed", failed)

    user = await make_user(db_session)
    user.stripe_customer_id = "cus_dunning"
    await db_session.commit()

    await billing_service._handle_payment_failed(
        db_session,
        {
            "id": "in_123",
            "customer": "cus_dunning",
            "amount_due": 999,
            "attempt_count": 2,
            "next_payment_attempt": 1786000000,
        },
    )
    assert len(failed.calls) == 1
    kwargs = failed.calls[0][1]
    assert kwargs["amount_usd"] == Decimal("9.99")  # cents → dollars
    assert kwargs["attempt"] == 2
    # Keyed per attempt: a redelivered webhook for attempt 2 can't double-send,
    # but attempt 3 is a genuinely new notice.
    assert kwargs["idempotency_key"] == "payment-failed-in_123-2"


@pytest.mark.asyncio
async def test_payment_failure_for_an_unknown_customer_is_a_no_op(
    db_session, monkeypatch
):
    failed = _Recorder()
    monkeypatch.setattr(email_service, "send_payment_failed", failed)

    await billing_service._handle_payment_failed(
        db_session, {"id": "in_x", "customer": "cus_nobody", "amount_due": 100}
    )
    assert failed.calls == []
