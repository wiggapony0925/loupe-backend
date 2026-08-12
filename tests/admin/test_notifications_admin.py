"""Router tests for `/v1/admin/notifications` — the push composer.

A broadcast reaches every phone at once and cannot be recalled, so the rules
that matter are the ones that stop an accident: the audience count is real
(not a guess), a dry run writes nothing, a test send reaches only the author,
and a mistyped category is refused rather than silently filed under something
else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.notification import Notification
from app.models.push_token import PushToken
from app.models.user import UserSettings
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


def _draft(**overrides) -> dict:
    payload = {"title": "Set 12 is live", "body": "Scan away.", "category": "news"}
    payload.update(overrides)
    return payload


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_composer_is_closed_to_anonymous_callers(client):
    """An open broadcast endpoint would let anyone buzz every phone we have."""
    assert_envelope_error(
        await client.get("/v1/admin/notifications/audience"), expected_status=401
    )
    assert_envelope_error(
        await client.post("/v1/admin/notifications", json=_draft()),
        expected_status=401,
    )


@pytest.mark.asyncio
async def test_composer_is_closed_to_ordinary_users(client, auth_headers):
    """Being signed in is not being staff."""
    for method, path in (
        ("get", "/v1/admin/notifications/audience"),
        ("get", "/v1/admin/notifications/log"),
    ):
        resp = await getattr(client, method)(path, headers=auth_headers)
        assert_envelope_error(resp, expected_status=403)

    for path in ("/v1/admin/notifications", "/v1/admin/notifications/test"):
        resp = await client.post(path, headers=auth_headers, json=_draft())
        assert_envelope_error(resp, expected_status=403)


# ── Audience ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audience_counts_only_accounts_that_can_receive(
    client, admin_headers, db_session
):
    """The composer says "N users · M devices" before you press send, so the
    count has to exclude the people a broadcast would skip anyway — banned and
    deleted accounts. Counting them would make every send look wider than it is."""
    reachable = await make_user(db_session)
    banned = await make_user(db_session)
    banned.banned_at = datetime.now(UTC)
    deleted = await make_user(db_session)
    deleted.deleted_at = datetime.now(UTC)
    db_session.add(
        PushToken(user_id=reachable.id, token=f"ExponentPushToken[{uuid.uuid4().hex}]")
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/notifications/audience", headers=admin_headers)
    )
    assert data["users"] == 2  # the admin + the reachable collector
    assert data["devices"] == 1
    assert data["push_enabled"] == 2


@pytest.mark.asyncio
async def test_audience_separates_devices_from_people(
    client, admin_headers, db_session
):
    """`devices` is registered push tokens, not users: one collector with a
    phone and a tablet is one person and two devices, and the gap between the
    two numbers is exactly "who never installed the app"."""
    collector = await make_user(db_session)
    db_session.add_all(
        [
            PushToken(
                user_id=collector.id, token=f"ExponentPushToken[{uuid.uuid4().hex}]"
            ),
            PushToken(
                user_id=collector.id, token=f"ExponentPushToken[{uuid.uuid4().hex}]"
            ),
        ]
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/notifications/audience", headers=admin_headers)
    )
    assert data["users"] == 2
    assert data["devices"] == 2


@pytest.mark.asyncio
async def test_audience_excludes_users_who_turned_push_off(
    client, admin_headers, db_session
):
    """`push_enabled` is the honest reach of a buzzing notification — someone
    who opted out still gets the inbox row but must not be counted as reached."""
    opted_out = await make_user(db_session)
    settings_row = await db_session.get(UserSettings, opted_out.id)
    assert settings_row is not None
    settings_row.push_notifications_enabled = False
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/notifications/audience", headers=admin_headers)
    )
    assert data["users"] == 2
    assert data["push_enabled"] == 1


# ── Sending ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_reports_the_reach_without_writing_anything(
    client, admin_headers, db_session
):
    """The preview button. It must be impossible for a preview to notify
    anyone — the whole point is to check the audience number first."""
    await make_user(db_session)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post(
            "/v1/admin/notifications",
            headers=admin_headers,
            json=_draft(dry_run=True),
        )
    )
    assert data == {"created": 0, "audience": 2, "dry_run": True}

    log = assert_envelope_ok(
        await client.get("/v1/admin/notifications/log", headers=admin_headers)
    )
    assert log["total"] == 0


@pytest.mark.asyncio
async def test_a_broadcast_reaches_every_active_account(
    client, admin_headers, db_session
):
    """One row per living account, including the sender. Banned and deleted
    users are skipped — messaging an account we suspended is the bug."""
    await make_user(db_session)
    banned = await make_user(db_session)
    banned.banned_at = datetime.now(UTC)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post(
            "/v1/admin/notifications", headers=admin_headers, json=_draft(push=False)
        )
    )
    assert data["created"] == 2
    assert data["audience"] == 2
    assert data["dry_run"] is False

    rows = (
        assert_envelope_ok(
            await client.get("/v1/admin/notifications/log", headers=admin_headers)
        )
    )["items"]
    assert {row["kind"] for row in rows} == {"admin_broadcast"}
    assert banned.id not in {uuid.UUID(row["id"]) for row in rows}


@pytest.mark.asyncio
async def test_a_targeted_send_reaches_exactly_one_person(
    client, admin_headers, db_session
):
    """ "Message this user" from the user drawer. Reporting the full audience
    here would make a one-to-one note look like a blast in the audit trail."""
    target = await make_user(db_session)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post(
            "/v1/admin/notifications",
            headers=admin_headers,
            json=_draft(user_id=str(target.id), push=False),
        )
    )
    assert data == {"created": 1, "audience": 1, "dry_run": False}

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == target.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.kind for row in rows] == ["admin_direct"]


@pytest.mark.asyncio
async def test_sending_to_an_unknown_user_is_a_404(client, admin_headers):
    """A stale user id in the composer must fail loudly rather than fall back
    to a broadcast — the failure mode of a silent fallback is unthinkable."""
    resp = await client.post(
        "/v1/admin/notifications",
        headers=admin_headers,
        json=_draft(user_id=str(uuid.uuid4())),
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_an_unknown_category_is_refused(client, admin_headers):
    """Categories are the user's filter tabs and their per-category mute
    settings. An unrecognised one would land in a tab nobody can see or mute."""
    resp = await client.post(
        "/v1/admin/notifications",
        headers=admin_headers,
        json=_draft(category="marketing"),
    )
    err = assert_envelope_error(resp, expected_status=422)
    assert "category" in str(err).lower()


@pytest.mark.asyncio
async def test_a_titleless_notification_is_refused(client, admin_headers):
    """The title is the only part guaranteed to show on a lock screen."""
    resp = await client.post(
        "/v1/admin/notifications", headers=admin_headers, json=_draft(title="")
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_test_send_goes_only_to_its_author(client, admin_headers, db_session):
    """The safety rail before a broadcast: see it on your own phone first.
    If this ever fanned out, the rail would be the accident."""
    bystander = await make_user(db_session)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post(
            "/v1/admin/notifications/test",
            headers=admin_headers,
            json=_draft(push=False),
        )
    )
    assert data == {"created": 1, "audience": 1, "dry_run": False}

    log = assert_envelope_ok(
        await client.get(
            "/v1/admin/notifications/log",
            headers=admin_headers,
            params={"user_id": str(bystander.id)},
        )
    )
    assert log["total"] == 0


@pytest.mark.asyncio
async def test_test_send_falls_back_to_the_system_category(client, admin_headers):
    """Unlike a real send, a test tolerates a junk category — it is a private
    dry run, and refusing it would block the very check that catches typos."""
    assert_envelope_ok(
        await client.post(
            "/v1/admin/notifications/test",
            headers=admin_headers,
            json=_draft(category="not-a-category", push=False),
        )
    )
    log = assert_envelope_ok(
        await client.get("/v1/admin/notifications/log", headers=admin_headers)
    )
    assert log["items"][0]["category"] == "system"
    assert log["items"][0]["kind"] == "admin_test"


# ── Log ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_lists_newest_first(client, admin_headers, db_session):
    """Newest-first is what makes the send an operator just made the first
    thing they see when they go looking for it."""
    target = await make_user(db_session)
    now = datetime.now(UTC)
    db_session.add_all(
        [
            Notification(
                user_id=target.id,
                category="news",
                kind="admin_direct",
                title="Older",
                created_at=now - timedelta(days=2),
            ),
            Notification(
                user_id=target.id,
                category="news",
                kind="admin_direct",
                title="Newer",
                created_at=now,
            ),
        ]
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/notifications/log", headers=admin_headers)
    )
    assert [row["title"] for row in data["items"]] == ["Newer", "Older"]


@pytest.mark.asyncio
async def test_log_can_be_scoped_to_one_user(client, admin_headers, db_session):
    """ "Why didn't they get it?" is answered by filtering the log to that
    person — so the filter must never fall back to showing everyone."""
    target = await make_user(db_session)
    bystander = await make_user(db_session)
    await db_session.commit()

    await client.post(
        "/v1/admin/notifications",
        headers=admin_headers,
        json=_draft(user_id=str(target.id), push=False),
    )
    await client.post(
        "/v1/admin/notifications",
        headers=admin_headers,
        json=_draft(user_id=str(bystander.id), push=False),
    )

    everything = assert_envelope_ok(
        await client.get("/v1/admin/notifications/log", headers=admin_headers)
    )
    assert everything["total"] == 2

    scoped = assert_envelope_ok(
        await client.get(
            "/v1/admin/notifications/log",
            headers=admin_headers,
            params={"user_id": str(target.id)},
        )
    )
    assert scoped["total"] == 1
    assert scoped["items"][0]["id"] is not None

    stranger = assert_envelope_ok(
        await client.get(
            "/v1/admin/notifications/log",
            headers=admin_headers,
            params={"user_id": str(uuid.uuid4())},
        )
    )
    assert stranger["total"] == 0 and stranger["items"] == []


@pytest.mark.asyncio
async def test_log_paginates_without_losing_the_total(
    client, admin_headers, db_session
):
    """`total` counts the whole log, not the page — an operator paging through
    a broadcast needs to know how far they are through it."""
    for _ in range(3):
        await make_user(db_session)
    await db_session.commit()
    await client.post(
        "/v1/admin/notifications", headers=admin_headers, json=_draft(push=False)
    )

    page = assert_envelope_ok(
        await client.get(
            "/v1/admin/notifications/log",
            headers=admin_headers,
            params={"page": 2, "page_size": 3},
        )
    )
    assert page["total"] == 4
    assert page["page"] == 2 and page["page_size"] == 3
    assert len(page["items"]) == 1


@pytest.mark.asyncio
async def test_log_reports_how_many_of_the_sent_rows_are_still_unread(
    client, admin_headers, db_session
):
    """`unread` counts the rows the log is showing that nobody has opened —
    it is how an operator tells whether a send landed. It follows the same
    filter as `total` (whole log, or one user when scoped), not just the
    current page."""
    target = await make_user(db_session)
    await db_session.commit()
    await client.post(
        "/v1/admin/notifications",
        headers=admin_headers,
        json=_draft(user_id=str(target.id), push=False),
    )

    log = assert_envelope_ok(
        await client.get("/v1/admin/notifications/log", headers=admin_headers)
    )
    assert log["total"] == 1
    assert log["items"][0]["read_at"] is None  # genuinely unread
    assert log["unread"] == 1

    # Once it is read, the count drops — and the scoped view agrees.
    row = await db_session.get(Notification, uuid.UUID(log["items"][0]["id"]))
    assert row is not None
    row.read_at = datetime.now(UTC)
    await db_session.commit()

    scoped = assert_envelope_ok(
        await client.get(
            "/v1/admin/notifications/log",
            headers=admin_headers,
            params={"user_id": str(target.id)},
        )
    )
    assert scoped["total"] == 1
    assert scoped["unread"] == 0
