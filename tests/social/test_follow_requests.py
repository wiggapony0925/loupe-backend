"""The private-account inbox, and the list of who someone follows.

Three rules are load-bearing here:

1. **A request belongs to the person it was sent to.** Nobody else can act
   on it — and the refusal is a 404, not a 403, because a 403 would tell a
   stranger that a particular request id exists.
2. **Accept creates the follow; decline creates nothing.** Both consume the
   request, so an inbox row can never be decided twice.
3. **A private account's follower/following lists are part of the vault.**
   Seeing who someone follows is seeing something about them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.notification import Notification
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _claim(client, user, username: str, **extra) -> dict:
    return assert_envelope_ok(
        await client.put(
            "/v1/social/me",
            json={"username": username, **extra},
            headers=_headers(user),
        )
    )


async def _request_to_follow(client, requester, handle: str) -> None:
    """Ask to follow a private account, asserting it lands as a request."""
    state = assert_envelope_ok(
        await client.post(
            f"/v1/social/users/{handle}/follow", headers=_headers(requester)
        )
    )
    assert state["relationship"] == "requested"


async def _inbox(client, owner) -> list[dict]:
    return assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(owner))
    )


# ── Accepting ──


@pytest.mark.asyncio
async def test_accepting_a_request_grants_the_follow_and_empties_the_inbox(
    client, created_user, second_user
):
    await _claim(client, created_user, "gatekeep1", is_private=True)
    await _claim(client, second_user, "asker1")
    await _request_to_follow(client, second_user, "gatekeep1")

    pending = await _inbox(client, created_user)
    assert [r["requester"]["username"] for r in pending] == ["asker1"]

    resp = await client.post(
        f"/v1/social/requests/{pending[0]['id']}/accept",
        headers=_headers(created_user),
    )
    assert resp.status_code == 204

    # The decision is consumed: the row is gone, and the follow is real.
    assert await _inbox(client, created_user) == []
    view = assert_envelope_ok(
        await client.get("/v1/social/users/gatekeep1", headers=_headers(second_user))
    )
    assert view["relationship"] == "following"
    assert view["follower_count"] == 1


@pytest.mark.asyncio
async def test_accepting_tells_the_person_who_asked(
    client, db_session, created_user, second_user
):
    """The requester is the only one who can't see the decision — they asked
    and then nothing visibly happens, so the acceptance has to be pushed."""
    await _claim(client, created_user, "gatekeep2", is_private=True)
    await _claim(client, second_user, "asker2")
    await _request_to_follow(client, second_user, "gatekeep2")
    pending = await _inbox(client, created_user)

    await client.post(
        f"/v1/social/requests/{pending[0]['id']}/accept",
        headers=_headers(created_user),
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == second_user.id,
                    Notification.kind == "social_follow_accepted",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_request_can_only_be_decided_once(client, created_user, second_user):
    """Accepting consumes the row, so a double-tap on the approve button
    can't create a second follow edge (the composite PK would raise)."""
    await _claim(client, created_user, "gatekeep3", is_private=True)
    await _claim(client, second_user, "asker3")
    await _request_to_follow(client, second_user, "gatekeep3")
    pending = await _inbox(client, created_user)

    first = await client.post(
        f"/v1/social/requests/{pending[0]['id']}/accept",
        headers=_headers(created_user),
    )
    assert first.status_code == 204
    assert_envelope_error(
        await client.post(
            f"/v1/social/requests/{pending[0]['id']}/accept",
            headers=_headers(created_user),
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_only_the_target_of_a_request_may_accept_it(
    client, db_session, created_user, second_user
):
    """A 404 rather than a 403: a 403 would confirm the id exists, which is
    already more than a bystander should learn about someone's inbox."""
    bystander = await make_user(db_session)
    await _claim(client, created_user, "gatekeep4", is_private=True)
    await _claim(client, second_user, "asker4")
    await _claim(client, bystander, "meddler4")
    await _request_to_follow(client, second_user, "gatekeep4")
    pending = await _inbox(client, created_user)

    for verb in ("accept", "decline"):
        assert_envelope_error(
            await client.post(
                f"/v1/social/requests/{pending[0]['id']}/{verb}",
                headers=_headers(bystander),
            ),
            expected_status=404,
        )
    # And the request survives the meddling.
    assert len(await _inbox(client, created_user)) == 1


# ── Declining ──


@pytest.mark.asyncio
async def test_declining_drops_the_request_without_creating_a_follow(
    client, created_user, second_user
):
    await _claim(client, created_user, "gatekeep5", is_private=True)
    await _claim(client, second_user, "asker5")
    await _request_to_follow(client, second_user, "gatekeep5")
    pending = await _inbox(client, created_user)

    resp = await client.post(
        f"/v1/social/requests/{pending[0]['id']}/decline",
        headers=_headers(created_user),
    )
    assert resp.status_code == 204

    assert await _inbox(client, created_user) == []
    view = assert_envelope_ok(
        await client.get("/v1/social/users/gatekeep5", headers=_headers(second_user))
    )
    assert view["relationship"] == "none"
    assert view["follower_count"] == 0
    assert view["can_view_collection"] is False


@pytest.mark.asyncio
async def test_declining_says_nothing_to_the_person_who_asked(
    client, db_session, created_user, second_user
):
    """A "you were declined" notification is a small cruelty and an invite
    to ask again — silence is the deliberate design here, so asking a second
    time stays possible."""
    await _claim(client, created_user, "gatekeep6", is_private=True)
    await _claim(client, second_user, "asker6")
    await _request_to_follow(client, second_user, "gatekeep6")
    pending = await _inbox(client, created_user)

    await client.post(
        f"/v1/social/requests/{pending[0]['id']}/decline",
        headers=_headers(created_user),
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == second_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []

    # Declined is not blocked: they may ask again.
    again = assert_envelope_ok(
        await client.post(
            "/v1/social/users/gatekeep6/follow", headers=_headers(second_user)
        )
    )
    assert again["relationship"] == "requested"


@pytest.mark.asyncio
async def test_deciding_a_request_that_does_not_exist_is_a_404(client, created_user):
    await _claim(client, created_user, "emptyinbox")
    for verb in ("accept", "decline"):
        assert_envelope_error(
            await client.post(
                f"/v1/social/requests/{uuid.uuid4()}/{verb}",
                headers=_headers(created_user),
            ),
            expected_status=404,
        )


@pytest.mark.asyncio
async def test_a_request_id_that_is_not_a_uuid_is_rejected(client, created_user):
    assert_envelope_error(
        await client.post(
            "/v1/social/requests/nonsense/accept", headers=_headers(created_user)
        ),
        expected_status=422,
    )


# ── Who someone follows ──


@pytest.mark.asyncio
async def test_the_following_list_names_everyone_that_collector_follows(
    client, db_session, created_user, second_user
):
    third = await make_user(db_session)
    await _claim(client, created_user, "sociable")
    await _claim(client, second_user, "followed1")
    await _claim(client, third, "followed2")
    for handle in ("followed1", "followed2"):
        await client.post(
            f"/v1/social/users/{handle}/follow", headers=_headers(created_user)
        )

    rows = assert_envelope_ok(
        await client.get(
            "/v1/social/users/sociable/following", headers=_headers(second_user)
        )
    )
    assert {r["username"] for r in rows} == {"followed1", "followed2"}
    # It is the FOLLOWING list, not the follower list: the subject is absent.
    assert "sociable" not in {r["username"] for r in rows}


@pytest.mark.asyncio
async def test_each_row_carries_the_viewers_own_relationship_not_the_subjects(
    client, db_session, created_user, second_user
):
    """The row draws a Follow button, and that button is about the VIEWER —
    reusing the subject's relationship would show "Following" on people the
    viewer has never followed."""
    third = await make_user(db_session)
    await _claim(client, created_user, "viewer1")
    await _claim(client, second_user, "subject1")
    await _claim(client, third, "shared1")
    await client.post("/v1/social/users/shared1/follow", headers=_headers(second_user))
    await client.post("/v1/social/users/shared1/follow", headers=_headers(created_user))

    rows = assert_envelope_ok(
        await client.get(
            "/v1/social/users/subject1/following", headers=_headers(created_user)
        )
    )
    assert [(r["username"], r["relationship"]) for r in rows] == [
        ("shared1", "following")
    ]


@pytest.mark.asyncio
async def test_following_nobody_is_an_empty_list_not_a_404(
    client, created_user, second_user
):
    await _claim(client, created_user, "hermit20")
    await _claim(client, second_user, "reader20")
    assert (
        assert_envelope_ok(
            await client.get(
                "/v1/social/users/hermit20/following", headers=_headers(second_user)
            )
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_private_accounts_following_list_is_closed_to_strangers(
    client, db_session, created_user, second_user
):
    """Who you follow is a fact about you. A private profile that still
    exposed this list would leak its interests to anyone who asked."""
    third = await make_user(db_session)
    await _claim(client, created_user, "closedbook", is_private=True)
    await _claim(client, second_user, "stranger20")
    await _claim(client, third, "friend20")
    await client.post(
        "/v1/social/users/friend20/follow", headers=_headers(created_user)
    )

    assert_envelope_error(
        await client.get(
            "/v1/social/users/closedbook/following", headers=_headers(second_user)
        ),
        expected_status=403,
    )
    # The owner always sees their own.
    mine = assert_envelope_ok(
        await client.get(
            "/v1/social/users/closedbook/following", headers=_headers(created_user)
        )
    )
    assert [r["username"] for r in mine] == ["friend20"]


@pytest.mark.asyncio
async def test_an_accepted_follower_may_read_a_private_following_list(
    client, db_session, created_user, second_user
):
    third = await make_user(db_session)
    await _claim(client, created_user, "closedbook2", is_private=True)
    await _claim(client, second_user, "approved2")
    await _claim(client, third, "friend2x")
    await client.post(
        "/v1/social/users/friend2x/follow", headers=_headers(created_user)
    )
    await client.post(
        "/v1/social/users/closedbook2/follow", headers=_headers(second_user)
    )
    pending = await _inbox(client, created_user)
    await client.post(
        f"/v1/social/requests/{pending[0]['id']}/accept",
        headers=_headers(created_user),
    )

    rows = assert_envelope_ok(
        await client.get(
            "/v1/social/users/closedbook2/following", headers=_headers(second_user)
        )
    )
    assert [r["username"] for r in rows] == ["friend2x"]


@pytest.mark.asyncio
async def test_a_banned_account_drops_out_of_the_following_list(
    client, db_session, created_user, second_user
):
    """The graph edge survives a ban, but the list is a list of people you
    can go and look at — and a banned account is not one of them."""
    banned = await make_user(db_session)
    await _claim(client, created_user, "follower30")
    await _claim(client, second_user, "reader30")
    await _claim(client, banned, "outlaw30")
    await client.post(
        "/v1/social/users/outlaw30/follow", headers=_headers(created_user)
    )

    banned.banned_at = datetime.now(UTC)
    await db_session.commit()

    rows = assert_envelope_ok(
        await client.get(
            "/v1/social/users/follower30/following", headers=_headers(second_user)
        )
    )
    assert rows == []


@pytest.mark.asyncio
async def test_the_following_list_of_an_unknown_handle_is_a_404(client, created_user):
    await _claim(client, created_user, "searcher40")
    assert_envelope_error(
        await client.get(
            "/v1/social/users/nobody-here/following", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_the_following_page_size_is_capped(client, created_user):
    """An uncapped limit is an invitation to pull the whole graph in one
    request."""
    await _claim(client, created_user, "greedy50")
    assert_envelope_error(
        await client.get(
            "/v1/social/users/greedy50/following?limit=500",
            headers=_headers(created_user),
        ),
        expected_status=422,
    )


# ── Auth boundary ──


@pytest.mark.asyncio
async def test_the_inbox_and_the_following_list_require_a_signed_in_caller(client):
    fake = uuid.uuid4()
    for method, path in (
        ("post", f"/v1/social/requests/{fake}/accept"),
        ("post", f"/v1/social/requests/{fake}/decline"),
        ("get", "/v1/social/users/anyone/following"),
    ):
        resp = await getattr(client, method)(path)
        assert resp.status_code == 401, f"{method.upper()} {path}"
