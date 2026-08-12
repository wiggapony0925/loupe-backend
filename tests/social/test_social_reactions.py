"""Reactions and the small reads around them: hearts, replies, reasons.

These are the endpoints a client calls the most and thinks about the least,
so the rules they have to keep are the boring ones:

1. **Idempotence** — a double tap is one like, and an unlike on something
   never liked is not an error. Clients retry these on flaky networks.
2. **The privacy gate is not optional on a WRITE.** Liking is how you find
   out something exists; a private account's post must 404 for a stranger
   on the like path exactly as it does on the read path.
3. **A closed list stays closed** — the report reasons are server-owned so
   the two clients can never offer different ones.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth.jwt import issue_token
from app.social.services.safety import REPORT_REASONS
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


async def _post(client, user, body: str = "hello") -> dict:
    return assert_envelope_ok(
        await client.post(
            "/v1/social/posts", data={"body": body}, headers=_headers(user)
        ),
        expected_status=201,
    )


async def _comment(client, user, post_id: str, body: str, parent_id=None) -> dict:
    payload: dict = {"body": body}
    if parent_id is not None:
        payload["parent_id"] = parent_id
    return assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post_id}/comments",
            json=payload,
            headers=_headers(user),
        ),
        expected_status=201,
    )


# ── Post likes ──


@pytest.mark.asyncio
async def test_unliking_a_post_you_never_liked_is_not_an_error(
    client, db_session, created_user
):
    """A client that retries a DELETE after a dropped response must not get
    a failure for the retry — the endpoint reports the state, not the
    transition."""
    author = await make_user(db_session)
    await _claim(client, author, "unlikeauthor")
    await _claim(client, created_user, "unliker")
    post = await _post(client, author, "never hearted")

    first = assert_envelope_ok(
        await client.delete(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    second = assert_envelope_ok(
        await client.delete(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    assert first == second == {"liked": False, "like_count": 0}


@pytest.mark.asyncio
async def test_a_post_like_count_sums_every_viewer(client, db_session, created_user):
    """The count comes back from the server on every tap so two people
    hearting at once can't leave either client showing 1."""
    author = await make_user(db_session)
    fan = await make_user(db_session)
    await _claim(client, author, "counted")
    await _claim(client, created_user, "fanone")
    await _claim(client, fan, "fantwo")
    post = await _post(client, author, "two hearts")

    await client.post(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )
    both = assert_envelope_ok(
        await client.post(f"/v1/social/posts/{post['id']}/like", headers=_headers(fan))
    )
    assert both == {"liked": True, "like_count": 2}

    # One person withdrawing leaves the other's like standing.
    left = assert_envelope_ok(
        await client.delete(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    assert left == {"liked": False, "like_count": 1}


@pytest.mark.asyncio
async def test_liking_a_post_that_does_not_exist_is_a_404(client, created_user):
    await _claim(client, created_user, "ghosthunter")
    assert_envelope_error(
        await client.post(
            f"/v1/social/posts/{uuid.uuid4()}/like", headers=_headers(created_user)
        ),
        expected_status=404,
    )
    assert_envelope_error(
        await client.delete(
            f"/v1/social/posts/{uuid.uuid4()}/like", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_post_id_that_is_not_a_uuid_is_rejected_before_any_lookup(
    client, created_user
):
    assert_envelope_error(
        await client.post(
            "/v1/social/posts/not-a-uuid/like", headers=_headers(created_user)
        ),
        expected_status=422,
    )


@pytest.mark.asyncio
async def test_unliking_a_private_accounts_post_stays_a_404_for_a_stranger(
    client, db_session, created_user
):
    """404 rather than 403 on the unlike path too: a 403 would confirm the
    post exists, which is the thing being kept private."""
    private = await make_user(db_session)
    await _claim(client, private, "vaulted", is_private=True)
    await _claim(client, created_user, "peeker")
    post = await _post(client, private, "behind the gate")

    assert_envelope_error(
        await client.delete(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        ),
        expected_status=404,
    )


# ── Comment likes ──


@pytest.mark.asyncio
async def test_liking_a_comment_twice_is_still_one_like(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "threadstarter")
    await _claim(client, created_user, "doubletapper")
    post = await _post(client, author, "post")
    comment = await _comment(client, author, post["id"], "first")

    first = assert_envelope_ok(
        await client.post(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        )
    )
    second = assert_envelope_ok(
        await client.post(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        )
    )
    assert first == second == {"liked": True, "like_count": 1}


@pytest.mark.asyncio
async def test_unliking_a_comment_clears_it_for_that_viewer_only(
    client, db_session, created_user
):
    author = await make_user(db_session)
    other = await make_user(db_session)
    await _claim(client, author, "commentauthor")
    await _claim(client, created_user, "hearter1")
    await _claim(client, other, "hearter2")
    post = await _post(client, author, "post")
    comment = await _comment(client, author, post["id"], "worth a heart")

    await client.post(
        f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
    )
    await client.post(
        f"/v1/social/comments/{comment['id']}/like", headers=_headers(other)
    )
    gone = assert_envelope_ok(
        await client.delete(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        )
    )
    assert gone == {"liked": False, "like_count": 1}

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/posts/{post['id']}/comments", headers=_headers(created_user)
        )
    )
    assert thread["items"][0]["viewer_has_liked"] is False
    assert thread["items"][0]["like_count"] == 1


@pytest.mark.asyncio
async def test_unliking_a_comment_never_liked_is_not_an_error(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "calmauthor")
    await _claim(client, created_user, "retrier")
    post = await _post(client, author, "post")
    comment = await _comment(client, author, post["id"], "unloved")

    assert assert_envelope_ok(
        await client.delete(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        )
    ) == {"liked": False, "like_count": 0}


@pytest.mark.asyncio
async def test_liking_a_comment_that_does_not_exist_is_a_404(client, created_user):
    await _claim(client, created_user, "comboghost")
    for method in (client.post, client.delete):
        assert_envelope_error(
            await method(
                f"/v1/social/comments/{uuid.uuid4()}/like",
                headers=_headers(created_user),
            ),
            expected_status=404,
        )


@pytest.mark.asyncio
async def test_a_stranger_cannot_like_a_comment_on_a_private_post(
    client, db_session, created_user
):
    """The comment's own row is readable, but the gate is the POST's — a
    heart is a write, and writing to a thread you can't read is a leak."""
    private = await make_user(db_session)
    await _claim(client, private, "sealedthread", is_private=True)
    await _claim(client, created_user, "outsider1")
    post = await _post(client, private, "private post")
    comment = await _comment(client, private, post["id"], "my own thread")

    assert_envelope_error(
        await client.post(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        ),
        expected_status=404,
    )


# ── Replies ──


@pytest.mark.asyncio
async def test_replies_read_oldest_first_and_report_the_full_total(
    client, db_session, created_user
):
    """The thread inlines only the first couple of replies; this endpoint is
    what "View all N replies" opens, so its total must be the REAL count and
    not the page's length."""
    author = await make_user(db_session)
    await _claim(client, author, "replyauthor")
    await _claim(client, created_user, "replier")
    post = await _post(client, author, "ask me anything")
    top = await _comment(client, author, post["id"], "top level")
    for n in range(4):
        await _comment(client, created_user, post["id"], f"reply {n}", top["id"])

    page = assert_envelope_ok(
        await client.get(
            f"/v1/social/comments/{top['id']}/replies?limit=2",
            headers=_headers(created_user),
        )
    )
    assert [c["body"] for c in page["items"]] == ["reply 0", "reply 1"]
    assert page["total"] == 4
    assert page["next_cursor"] == "2"
    # Every row is pinned to the top-level comment, never to a sibling reply.
    assert {c["parent_id"] for c in page["items"]} == {top["id"]}

    rest = assert_envelope_ok(
        await client.get(
            f"/v1/social/comments/{top['id']}/replies?limit=2&offset=2",
            headers=_headers(created_user),
        )
    )
    assert [c["body"] for c in rest["items"]] == ["reply 2", "reply 3"]
    # Nothing beyond the last page, so no cursor to follow.
    assert rest["next_cursor"] is None


@pytest.mark.asyncio
async def test_a_comment_with_no_replies_returns_an_empty_thread(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "lonelyauthor")
    await _claim(client, created_user, "lonelyreader")
    post = await _post(client, author, "post")
    comment = await _comment(client, author, post["id"], "no takers")

    page = assert_envelope_ok(
        await client.get(
            f"/v1/social/comments/{comment['id']}/replies",
            headers=_headers(created_user),
        )
    )
    assert page == {"items": [], "next_cursor": None, "total": 0}


@pytest.mark.asyncio
async def test_replies_carry_the_viewers_own_like_state(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "likedreply")
    await _claim(client, created_user, "replyliker")
    post = await _post(client, author, "post")
    top = await _comment(client, author, post["id"], "top")
    reply = await _comment(client, author, post["id"], "a reply", top["id"])
    await client.post(
        f"/v1/social/comments/{reply['id']}/like", headers=_headers(created_user)
    )

    page = assert_envelope_ok(
        await client.get(
            f"/v1/social/comments/{top['id']}/replies", headers=_headers(created_user)
        )
    )
    assert page["items"][0]["viewer_has_liked"] is True
    assert page["items"][0]["like_count"] == 1
    # Someone else's comment is not yours to remove.
    assert page["items"][0]["can_delete"] is False


@pytest.mark.asyncio
async def test_replies_for_an_unknown_comment_are_a_404(client, created_user):
    await _claim(client, created_user, "replyghost")
    assert_envelope_error(
        await client.get(
            f"/v1/social/comments/{uuid.uuid4()}/replies",
            headers=_headers(created_user),
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_stranger_cannot_read_replies_under_a_private_post(
    client, db_session, created_user
):
    private = await make_user(db_session)
    await _claim(client, private, "sealedreplies", is_private=True)
    await _claim(client, created_user, "outsider2")
    post = await _post(client, private, "private")
    top = await _comment(client, private, post["id"], "top")
    await _comment(client, private, post["id"], "reply", top["id"])

    assert_envelope_error(
        await client.get(
            f"/v1/social/comments/{top['id']}/replies", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_negative_reply_offset_is_rejected(client, db_session, created_user):
    author = await make_user(db_session)
    await _claim(client, author, "offsetauthor")
    await _claim(client, created_user, "offsetreader")
    post = await _post(client, author, "post")
    comment = await _comment(client, author, post["id"], "top")

    assert_envelope_error(
        await client.get(
            f"/v1/social/comments/{comment['id']}/replies?offset=-1",
            headers=_headers(created_user),
        ),
        expected_status=422,
    )


# ── Report reasons ──


@pytest.mark.asyncio
async def test_report_reasons_are_the_closed_server_owned_list(client, auth_headers):
    """Both clients render THIS list, so a reason one can send and the other
    can't never exists. It also has to match what the report endpoint will
    accept — a label with no matching key is a dead menu item."""
    reasons = assert_envelope_ok(
        await client.get("/v1/social/report-reasons", headers=auth_headers)
    )
    assert reasons == dict(REPORT_REASONS)
    assert set(reasons) == {
        "spam",
        "nudity",
        "hate",
        "violence",
        "counterfeit",
        "other",
    }
    assert all(isinstance(label, str) and label for label in reasons.values())


@pytest.mark.asyncio
async def test_every_offered_reason_is_accepted_by_the_report_endpoint(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "reported")
    await _claim(client, created_user, "reporter")
    reasons = assert_envelope_ok(
        await client.get("/v1/social/report-reasons", headers=_headers(created_user))
    )

    for reason in reasons:
        post = await _post(client, author, f"about {reason}")
        case = assert_envelope_ok(
            await client.post(
                "/v1/social/reports",
                json={
                    "target_type": "post",
                    "target_id": post["id"],
                    "reason": reason,
                },
                headers=_headers(created_user),
            ),
            expected_status=201,
        )
        assert case["reason"] == reason


# ── Auth boundary ──


@pytest.mark.asyncio
async def test_reactions_and_their_reads_all_require_a_signed_in_caller(client):
    """Even the reason list: it is behind auth because only a signed-in user
    can file a report, and an open endpoint is one more thing to rate-limit."""
    fake = uuid.uuid4()
    for method, path in (
        ("post", f"/v1/social/posts/{fake}/like"),
        ("delete", f"/v1/social/posts/{fake}/like"),
        ("post", f"/v1/social/comments/{fake}/like"),
        ("delete", f"/v1/social/comments/{fake}/like"),
        ("get", f"/v1/social/comments/{fake}/replies"),
        ("get", "/v1/social/report-reasons"),
    ):
        resp = await getattr(client, method)(path)
        assert resp.status_code == 401, f"{method.upper()} {path}"
