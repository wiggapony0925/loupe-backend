"""Router tests for `/v1/admin/waitlist` — the Loupe Scanner signup pipeline.

The signup confirmation promises "we'll email you when your spot opens up", so
the interesting rule is not the CRUD: it is that moving someone to ``invited``
is that promise being kept, exactly once, and that re-saving the same stage
does not mail them again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.enums import WaitlistStatusEnum
from app.models.waitlist import WaitlistEntry
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


@pytest.fixture
def invites_sent(monkeypatch) -> list[str]:
    """Capture invite emails instead of sending them."""
    sent: list[str] = []

    async def _capture(email: str, *, name: str | None = None) -> bool:
        sent.append(email)
        return True

    monkeypatch.setattr(
        "app.services.portal.waitlist_service.email_service.send_waitlist_invite",
        _capture,
    )
    return sent


async def _entry(
    db,
    *,
    status: str = WaitlistStatusEnum.waiting.value,
    created_at: datetime | None = None,
) -> WaitlistEntry:
    row = WaitlistEntry(
        email=f"w+{uuid.uuid4().hex[:8]}@example.com",
        name="Collector",
        quantity=1,
        status=status,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    await db.flush()
    return row


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waitlist_is_not_public(client):
    """The list is a roster of email addresses — the classic scrape target."""
    assert_envelope_error(await client.get("/v1/admin/waitlist"), expected_status=401)


@pytest.mark.asyncio
async def test_waitlist_is_closed_to_ordinary_users(client, auth_headers, db_session):
    """Every verb, not just the read: an ordinary account must not be able to
    invite themselves to the front of the queue or delete a rival."""
    entry = await _entry(db_session)
    await db_session.commit()

    assert_envelope_error(
        await client.get("/v1/admin/waitlist", headers=auth_headers),
        expected_status=403,
    )
    assert_envelope_error(
        await client.patch(
            f"/v1/admin/waitlist/{entry.id}/status",
            headers=auth_headers,
            json={"status": "invited"},
        ),
        expected_status=403,
    )
    assert_envelope_error(
        await client.delete(f"/v1/admin/waitlist/{entry.id}", headers=auth_headers),
        expected_status=403,
    )


# ── Listing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listing_is_empty_before_the_first_signup(client, admin_headers):
    """A product page that hasn't launched yet still has to render its portal."""
    assert (
        assert_envelope_ok(
            await client.get("/v1/admin/waitlist", headers=admin_headers)
        )
        == []
    )


@pytest.mark.asyncio
async def test_listing_returns_newest_signups_first(client, admin_headers, db_session):
    """Operators work the top of the list, so "newest" has to mean newest —
    ordering by insertion would put the oldest signup in front of them."""
    now = datetime.now(UTC)
    old = await _entry(db_session, created_at=now - timedelta(days=3))
    new = await _entry(db_session, created_at=now)
    await db_session.commit()

    rows = assert_envelope_ok(
        await client.get("/v1/admin/waitlist", headers=admin_headers)
    )
    assert [row["email"] for row in rows] == [new.email, old.email]


@pytest.mark.asyncio
async def test_listing_filters_by_pipeline_stage(client, admin_headers, db_session):
    """ "Who still needs an invite?" is the daily question — the answer is the
    `waiting` filter, and it must not include people already invited."""
    waiting = await _entry(db_session)
    await _entry(db_session, status=WaitlistStatusEnum.invited.value)
    await _entry(db_session, status=WaitlistStatusEnum.purchased.value)
    await db_session.commit()

    rows = assert_envelope_ok(
        await client.get(
            "/v1/admin/waitlist", headers=admin_headers, params={"status": "waiting"}
        )
    )
    assert [row["email"] for row in rows] == [waiting.email]


# ── Advancing a signup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inviting_a_signup_sends_the_promised_email(
    client, admin_headers, db_session, invites_sent
):
    """The confirmation email promised "we'll email you when your spot opens
    up". Reaching `invited` is that moment — the mail is the feature."""
    entry = await _entry(db_session)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/waitlist/{entry.id}/status",
            headers=admin_headers,
            json={"status": "invited"},
        )
    )
    assert data["status"] == "invited"
    assert invites_sent == [entry.email]


@pytest.mark.asyncio
async def test_re_saving_invited_does_not_email_again(
    client, admin_headers, db_session, invites_sent
):
    """Only the *transition* mails. Without that rule, an operator tidying the
    board — or a double-clicked button — spams someone who already has their
    invite."""
    entry = await _entry(db_session, status=WaitlistStatusEnum.invited.value)
    await db_session.commit()

    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/waitlist/{entry.id}/status",
            headers=admin_headers,
            json={"status": "invited"},
        )
    )
    assert invites_sent == []


@pytest.mark.asyncio
async def test_marking_a_signup_purchased_is_silent(
    client, admin_headers, db_session, invites_sent
):
    """Only `invited` carries a promise. A buyer already heard from us through
    the checkout, so closing out their row must not re-invite them."""
    entry = await _entry(db_session, status=WaitlistStatusEnum.invited.value)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/waitlist/{entry.id}/status",
            headers=admin_headers,
            json={"status": "purchased"},
        )
    )
    assert data["status"] == "purchased"
    assert invites_sent == []


@pytest.mark.asyncio
async def test_advancing_an_unknown_signup_is_a_404(client, admin_headers):
    """Never resurrect a deleted signup by updating it."""
    resp = await client.patch(
        f"/v1/admin/waitlist/{uuid.uuid4()}/status",
        headers=admin_headers,
        json={"status": "invited"},
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_stage_outside_the_pipeline_is_refused(
    client, admin_headers, db_session
):
    """The stage drives both the roster filters and the invite mail, so an
    unrecognised value would strand a signup in a state nothing queries."""
    entry = await _entry(db_session)
    await db_session.commit()

    resp = await client.patch(
        f"/v1/admin/waitlist/{entry.id}/status",
        headers=admin_headers,
        json={"status": "shipped"},
    )
    assert_envelope_error(resp, expected_status=422)


# ── Removing spam ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_signup_removes_the_row(client, admin_headers, db_session):
    """Spam signups are deleted outright rather than soft-flagged: the roster
    is an email list, and a "removed" row we still hold is still a liability."""
    entry = await _entry(db_session)
    await db_session.commit()
    entry_id = entry.id

    resp = await client.delete(f"/v1/admin/waitlist/{entry_id}", headers=admin_headers)
    assert resp.status_code == 204

    remaining = (
        (
            await db_session.execute(
                select(WaitlistEntry).where(WaitlistEntry.id == entry_id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_deleting_an_unknown_signup_is_a_404(client, admin_headers):
    """A double-clicked delete should report "already gone", not pretend to
    have removed something."""
    resp = await client.delete(
        f"/v1/admin/waitlist/{uuid.uuid4()}", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)
