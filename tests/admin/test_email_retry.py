"""Router tests for `POST /v1/admin/email/log/{id}/retry`.

Retry re-sends the *stored render*, not a freshly built message — so a support
agent can push out the exact email a user was supposed to receive. That makes
the guards around it the interesting part: only a send that actually failed
(or is stuck in the queue) may be replayed, and only if we still hold what was
rendered. Replaying a delivered email would double-mail a real person.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
import pytest_asyncio

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.email_log import EmailLog
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest_asyncio.fixture
async def admin_user(db_session):
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@contextlib.contextmanager
def _provider_configured():
    """Turn the provider on for the duration of a test. Tests run with email
    forced off (see tests/conftest.py), which is its own code path here."""
    settings = get_settings()
    prev = (settings.resend_api_key, settings.notifications_from_email)
    settings.resend_api_key = "re_test"  # type: ignore[misc]
    settings.notifications_from_email = "hello@example.com"  # type: ignore[misc]
    try:
        yield
    finally:
        (
            settings.resend_api_key,  # type: ignore[misc]
            settings.notifications_from_email,  # type: ignore[misc]
        ) = prev


async def _log(
    db, *, status: str = "failed", html: str | None = "<p>Hi</p>"
) -> EmailLog:
    row = EmailLog(
        to_email="collector@example.com",
        category="market",
        subject="Your price alert",
        html=html,
        text="Hi",
        headers={"X-Entity-Ref-ID": "abc"},
        from_email="alerts@example.com",
        status=status,
        error="provider timeout" if status == "failed" else None,
    )
    db.add(row)
    await db.flush()
    return row


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_is_not_public(client, db_session):
    """The endpoint sends real mail to a stored address — anonymous access
    would be an open relay pointed at our own users."""
    row = await _log(db_session)
    await db_session.commit()
    resp = await client.post(f"/v1/admin/email/log/{row.id}/retry")
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_retry_is_closed_to_ordinary_users(client, auth_headers, db_session):
    """A signed-in collector must not be able to replay somebody else's mail."""
    row = await _log(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/v1/admin/email/log/{row.id}/retry", headers=auth_headers
    )
    assert_envelope_error(resp, expected_status=403)


# ── Guards ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrying_an_unknown_log_entry_is_a_404(client, admin_headers):
    """A stale id from an old dashboard tab must not send anything."""
    resp = await client.post(
        f"/v1/admin/email/log/{uuid.uuid4()}/retry", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.parametrize("status", ["sent", "delivered", "bounced", "complained"])
@pytest.mark.asyncio
async def test_only_failed_or_stuck_sends_may_be_replayed(
    client, admin_headers, db_session, status
):
    """A delivered email already reached a real person; re-sending it is a
    duplicate in their inbox. A bounce would just bounce again. The 409 is
    what turns "retry" into a repair tool rather than a resend button."""
    row = await _log(db_session, status=status)
    await db_session.commit()

    resp = await client.post(
        f"/v1/admin/email/log/{row.id}/retry", headers=admin_headers
    )
    err = assert_envelope_error(resp, expected_status=409)
    assert status in str(err)


@pytest.mark.asyncio
async def test_a_log_row_without_a_stored_render_cannot_be_retried(
    client, admin_headers, db_session
):
    """Retry replays the stored HTML. With nothing stored there is nothing to
    send — rebuilding it from scratch would silently change what the user
    receives compared with what the log claims we sent."""
    row = await _log(db_session, html=None)
    await db_session.commit()

    resp = await client.post(
        f"/v1/admin/email/log/{row.id}/retry", headers=admin_headers
    )
    err = assert_envelope_error(resp, expected_status=409)
    assert "stored render" in str(err)


# ── Sending ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_without_a_configured_provider_reports_rather_than_raises(
    client, admin_headers, db_session
):
    """On a deployment with no email provider (or a local dev box) the portal
    should say so plainly instead of 500-ing — the operator's next step is a
    config change, not a bug report."""
    row = await _log(db_session)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post(f"/v1/admin/email/log/{row.id}/retry", headers=admin_headers)
    )
    assert data["sent"] is False
    assert data["to"] == "collector@example.com"
    assert "not configured" in data["detail"]


@pytest.mark.asyncio
async def test_retry_replays_the_stored_render_to_the_original_recipient(
    client, admin_headers, db_session, monkeypatch
):
    """Every part of the original send is reused — recipient, subject, body,
    headers, sender, and the log row itself — so the retry updates the
    existing paper trail instead of opening a second one."""
    captured: dict = {}

    async def _capture(to, subject, html, text=None, **kwargs):
        captured.update({"to": to, "subject": subject, "html": html, "text": text})
        captured.update(kwargs)
        return True

    monkeypatch.setattr("app.services.email_service.send_email", _capture)

    row = await _log(db_session)
    await db_session.commit()
    row_id = row.id

    with _provider_configured():
        data = assert_envelope_ok(
            await client.post(
                f"/v1/admin/email/log/{row_id}/retry", headers=admin_headers
            )
        )

    assert data == {
        "sent": True,
        "to": "collector@example.com",
        "detail": "Re-sent.",
    }
    assert captured["to"] == "collector@example.com"
    assert captured["subject"] == "Your price alert"
    assert captured["html"] == "<p>Hi</p>"
    assert captured["headers"] == {"X-Entity-Ref-ID": "abc"}
    assert captured["from_email"] == "alerts@example.com"
    assert captured["log_id"] == row_id


@pytest.mark.asyncio
async def test_a_queued_row_can_be_unstuck(
    client, admin_headers, db_session, monkeypatch
):
    """`queued` means the pipeline started a send and never finished it — a
    crashed worker. Those are exactly the rows an operator needs to push
    through by hand."""

    async def _ok(*args, **kwargs):
        return True

    monkeypatch.setattr("app.services.email_service.send_email", _ok)

    row = await _log(db_session, status="queued")
    await db_session.commit()

    with _provider_configured():
        data = assert_envelope_ok(
            await client.post(
                f"/v1/admin/email/log/{row.id}/retry", headers=admin_headers
            )
        )
    assert data["sent"] is True


@pytest.mark.asyncio
async def test_a_rejected_retry_says_so_instead_of_claiming_success(
    client, admin_headers, db_session, monkeypatch
):
    """A provider that refuses the message twice is a real signal — reporting
    "sent" anyway would hide a broken template or a suppressed address."""

    async def _rejected(*args, **kwargs):
        return False

    monkeypatch.setattr("app.services.email_service.send_email", _rejected)

    row = await _log(db_session)
    await db_session.commit()

    with _provider_configured():
        data = assert_envelope_ok(
            await client.post(
                f"/v1/admin/email/log/{row.id}/retry", headers=admin_headers
            )
        )
    assert data["sent"] is False
    assert "rejected" in data["detail"]
