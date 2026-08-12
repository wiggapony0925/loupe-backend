"""The inbox's three small endpoints: badge count, header summary, mark-read.

The badge and the filter strip are drawn on every app focus, and read state
lives here rather than on the device — so these have to be right per-user and
per-category, or one person's badge shows another's mail.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.jwt import issue_token
from app.services import notification_service
from tests.conftest import assert_envelope_error, assert_envelope_ok


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _notify(db, user, *, category: str = "system", title: str = "Hello"):
    row = await notification_service.notify(
        db,
        user.id,
        category=category,
        kind="announcement",
        title=title,
        push=False,
    )
    assert row is not None
    return row


# ── GET /v1/me/notifications/unread-count ─────────────────────────────────


@pytest.mark.asyncio
async def test_unread_count_is_zero_for_a_fresh_account(client, created_user):
    data = assert_envelope_ok(
        await client.get(
            "/v1/me/notifications/unread-count", headers=_headers(created_user)
        )
    )
    assert data["unread"] == 0


@pytest.mark.asyncio
async def test_unread_count_only_counts_my_own_notifications(
    client, created_user, second_user, db_session
):
    """The badge is the most visible number in the app; counting someone else's
    rows would leak the fact that they were messaged at all."""
    await _notify(db_session, created_user, title="Mine")
    await _notify(db_session, second_user, title="Theirs")
    await _notify(db_session, second_user, title="Theirs too")

    mine = assert_envelope_ok(
        await client.get(
            "/v1/me/notifications/unread-count", headers=_headers(created_user)
        )
    )
    theirs = assert_envelope_ok(
        await client.get(
            "/v1/me/notifications/unread-count", headers=_headers(second_user)
        )
    )
    assert mine["unread"] == 1
    assert theirs["unread"] == 2


@pytest.mark.asyncio
async def test_unread_count_requires_a_session(client):
    resp = await client.get("/v1/me/notifications/unread-count")
    assert_envelope_error(resp, expected_status=401)


# ── GET /v1/me/notifications/summary ──────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_serves_the_category_catalogue_even_when_empty(
    client, created_user
):
    """Clients render the filter strip straight from this response — labels,
    icons and order included — so it must arrive complete on an empty inbox,
    not collapse to whatever categories happen to have mail."""
    data = assert_envelope_ok(
        await client.get("/v1/me/notifications/summary", headers=_headers(created_user))
    )

    assert data["unread"] == 0
    keys = [c["key"] for c in data["categories"]]
    assert keys == ["social", "market", "news", "system"]
    for cat in data["categories"]:
        assert cat["label"] and cat["description"] and cat["icon"] and cat["tone"]
        assert cat["unread"] == 0


@pytest.mark.asyncio
async def test_summary_counts_unread_per_category(client, created_user, db_session):
    await _notify(
        db_session, created_user, category="market", title="Charizard hit $500"
    )
    await _notify(db_session, created_user, category="market", title="Pikachu hit $20")
    await _notify(db_session, created_user, category="social", title="New follower")

    data = assert_envelope_ok(
        await client.get("/v1/me/notifications/summary", headers=_headers(created_user))
    )
    counts = {c["key"]: c["unread"] for c in data["categories"]}

    assert data["unread"] == 3
    assert counts["market"] == 2
    assert counts["social"] == 1
    assert counts["news"] == 0


@pytest.mark.asyncio
async def test_billing_notifications_are_counted_under_account(
    client, created_user, db_session
):
    """``billing`` is a real stored category (receipts stay queryable) but has no
    tab of its own. It must fold into Account, or the tab badges stop adding up
    to the app-icon badge and a receipt becomes invisible."""
    await _notify(db_session, created_user, category="billing", title="Receipt")

    data = assert_envelope_ok(
        await client.get("/v1/me/notifications/summary", headers=_headers(created_user))
    )
    counts = {c["key"]: c["unread"] for c in data["categories"]}

    assert data["unread"] == 1
    assert counts["system"] == 1
    assert sum(counts.values()) == data["unread"]


@pytest.mark.asyncio
async def test_summary_ignores_notifications_already_read(
    client, created_user, db_session
):
    row = await _notify(db_session, created_user, category="news", title="New post")
    await notification_service.mark_read(db_session, created_user.id, [row.id])

    data = assert_envelope_ok(
        await client.get("/v1/me/notifications/summary", headers=_headers(created_user))
    )
    counts = {c["key"]: c["unread"] for c in data["categories"]}
    assert data["unread"] == 0
    assert counts["news"] == 0


@pytest.mark.asyncio
async def test_summary_requires_a_session(client):
    resp = await client.get("/v1/me/notifications/summary")
    assert_envelope_error(resp, expected_status=401)


# ── POST /v1/me/notifications/read ────────────────────────────────────────


@pytest.mark.asyncio
async def test_marking_ids_read_returns_the_new_badge_count(
    client, created_user, db_session
):
    """The response is the resulting badge so the client can repaint without a
    second round trip — the reason this returns a count at all."""
    first = await _notify(db_session, created_user, title="One")
    await _notify(db_session, created_user, title="Two")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read",
            headers=_headers(created_user),
            json={"ids": [str(first.id)]},
        )
    )
    assert data["unread"] == 1


@pytest.mark.asyncio
async def test_marking_all_read_clears_the_badge(client, created_user, db_session):
    await _notify(db_session, created_user, title="One")
    await _notify(db_session, created_user, category="market", title="Two")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read",
            headers=_headers(created_user),
            json={"all": True},
        )
    )
    assert data["unread"] == 0


@pytest.mark.asyncio
async def test_all_beats_a_supplied_id_list(client, created_user, db_session):
    """``ids`` is documented as ignored when ``all`` is true — a client that
    sends both must still get the whole inbox cleared, not just those rows."""
    first = await _notify(db_session, created_user, title="One")
    await _notify(db_session, created_user, title="Two")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read",
            headers=_headers(created_user),
            json={"ids": [str(first.id)], "all": True},
        )
    )
    assert data["unread"] == 0


@pytest.mark.asyncio
async def test_marking_all_read_stops_at_my_own_inbox(
    client, created_user, second_user, db_session
):
    """ "Clear all" is the easiest place to write an unscoped UPDATE — one
    missing ``user_id`` predicate and every user in the table loses their
    badge."""
    await _notify(db_session, created_user, title="Mine")
    await _notify(db_session, second_user, title="Theirs")

    await client.post(
        "/v1/me/notifications/read",
        headers=_headers(created_user),
        json={"all": True},
    )

    theirs = assert_envelope_ok(
        await client.get(
            "/v1/me/notifications/unread-count", headers=_headers(second_user)
        )
    )
    assert theirs["unread"] == 1


@pytest.mark.asyncio
async def test_i_cannot_mark_another_users_notification_read(
    client, created_user, second_user, db_session
):
    """Notification ids are the only thing standing between two inboxes here —
    posting someone else's id must silently do nothing, not clear their row."""
    theirs = await _notify(db_session, second_user, title="Theirs")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read",
            headers=_headers(created_user),
            json={"ids": [str(theirs.id)]},
        )
    )
    assert data["unread"] == 0  # the caller had none of their own

    still_unread = assert_envelope_ok(
        await client.get(
            "/v1/me/notifications/unread-count", headers=_headers(second_user)
        )
    )
    assert still_unread["unread"] == 1


@pytest.mark.asyncio
async def test_an_unknown_id_is_harmless(client, created_user, db_session):
    """A stale id from a device that synced before a deletion must not 404 the
    whole batch — the rest of the request still has to land."""
    mine = await _notify(db_session, created_user, title="Mine")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read",
            headers=_headers(created_user),
            json={"ids": [str(uuid.uuid4()), str(mine.id)]},
        )
    )
    assert data["unread"] == 0


@pytest.mark.asyncio
async def test_an_empty_request_is_accepted_and_changes_nothing(
    client, created_user, db_session
):
    await _notify(db_session, created_user, title="Mine")

    data = assert_envelope_ok(
        await client.post(
            "/v1/me/notifications/read", headers=_headers(created_user), json={}
        )
    )
    assert data["unread"] == 1


@pytest.mark.asyncio
async def test_a_non_uuid_id_is_rejected(client, created_user):
    resp = await client.post(
        "/v1/me/notifications/read",
        headers=_headers(created_user),
        json={"ids": ["not-a-uuid"]},
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_mark_read_requires_a_session(client):
    resp = await client.post("/v1/me/notifications/read", json={"all": True})
    assert_envelope_error(resp, expected_status=401)
