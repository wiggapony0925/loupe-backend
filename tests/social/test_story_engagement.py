"""Stories: who watched, who replied, and who is allowed to ask.

The tray and expiry rules live in ``test_stories.py``; this file is about
the three engagement endpoints hanging off one story, where the mistakes are
quieter:

* a viewer list is the most private thing in the feature — it is the one
  place that says who was LOOKING — so only the author may read it, and a
  story nobody may see must not even confirm it exists;
* marking seen is fire-and-forget from the client, so it has to be safe to
  repeat and safe to lose;
* a story comment is public to everyone who can see the story, which means
  its gate is the story's gate and nothing weaker.
"""

from __future__ import annotations

import io
import struct
import uuid
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.social.models import SocialStory, SocialStoryView
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _make_admin(db_session):
    """A Loupe staff account — the moderation-queue caller.

    A fresh user rather than promoting one of the fixtures, so the same test
    can assert an ordinary account is refused the admin route it exercises.
    """
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _claim(client, user, username: str, **extra) -> dict:
    return assert_envelope_ok(
        await client.put(
            "/v1/social/me",
            json={"username": username, **extra},
            headers=_headers(user),
        )
    )


def _png(width: int = 6, height: int = 9) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _post_story(client, user, *, caption: str | None = None) -> dict:
    data = {}
    if caption is not None:
        data["caption"] = caption
    return assert_envelope_ok(
        await client.post(
            "/v1/social/stories",
            data=data,
            files={"media": ("s.png", io.BytesIO(_png()), "image/png")},
            headers=_headers(user),
        ),
        expected_status=201,
    )


async def _expire(db_session, story_id: str) -> None:
    row = (
        await db_session.execute(
            select(SocialStory).where(SocialStory.id == uuid.UUID(story_id))
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()


# ── Marking seen ──


@pytest.mark.asyncio
async def test_marking_a_story_seen_clears_the_ring_for_that_viewer_only(
    client, db_session, created_user, second_user
):
    """`seen` is what the tray's ring is drawn from, so it has to be per
    viewer — one person watching must not dim it for everyone."""
    third = await make_user(db_session)
    await _claim(client, created_user, "teller1")
    await _claim(client, second_user, "watcher1")
    await _claim(client, third, "watcher2")
    story = await _post_story(client, created_user)

    resp = await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
    )
    assert resp.status_code == 204

    watched = assert_envelope_ok(
        await client.get("/v1/social/stories/teller1", headers=_headers(second_user))
    )
    unwatched = assert_envelope_ok(
        await client.get("/v1/social/stories/teller1", headers=_headers(third))
    )
    assert watched[0]["seen"] is True
    assert unwatched[0]["seen"] is False


@pytest.mark.asyncio
async def test_re_watching_a_story_stays_a_204_and_records_one_row(
    client, db_session, created_user, second_user
):
    """Clients fire this on every open and never look at the response — a
    second call must not surface as an error the user can't act on."""
    await _claim(client, created_user, "teller2")
    await _claim(client, second_user, "rewatcher")
    story = await _post_story(client, created_user)

    for _ in range(3):
        resp = await client.post(
            f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
        )
        assert resp.status_code == 204

    rows = (
        (
            await db_session.execute(
                select(SocialStoryView).where(
                    SocialStoryView.story_id == uuid.UUID(story["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_the_author_watching_their_own_story_records_nothing(
    client, db_session, created_user
):
    """Otherwise every story reads "1 view" the second it is posted, and the
    author appears in their own viewer list."""
    await _claim(client, created_user, "teller3")
    story = await _post_story(client, created_user)

    resp = await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(created_user)
    )
    assert resp.status_code == 204

    rows = (
        (
            await db_session.execute(
                select(SocialStoryView).where(
                    SocialStoryView.story_id == uuid.UUID(story["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_marking_an_unknown_story_seen_is_a_404(client, created_user):
    await _claim(client, created_user, "ghostwatcher")
    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{uuid.uuid4()}/view", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_stranger_cannot_mark_a_private_story_seen(
    client, created_user, second_user
):
    """The view row is what an author's viewer list is built from — letting
    an outsider write one would put a name in front of the author that the
    story was never shown to."""
    await _claim(client, created_user, "hermit30", is_private=True)
    await _claim(client, second_user, "stranger30")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_an_expired_story_can_no_longer_be_marked_seen(
    client, db_session, created_user, second_user
):
    """Expiry is a predicate on every read, and this write reads first — so
    a client that was mid-tap when the clock ticked over gets a 404 rather
    than adding a view to a story that is over."""
    await _claim(client, created_user, "teller4")
    await _claim(client, second_user, "latecomer")
    story = await _post_story(client, created_user)
    await _expire(db_session, story["id"])

    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
        ),
        expected_status=404,
    )


# ── The viewer list ──


@pytest.mark.asyncio
async def test_the_viewer_list_reads_most_recent_first(
    client, db_session, created_user, second_user
):
    """The author opens this to see who just watched, so the newest name
    belongs at the top."""
    early = await make_user(db_session)
    await _claim(client, created_user, "teller5")
    await _claim(client, second_user, "recent5")
    await _claim(client, early, "early5")
    story = await _post_story(client, created_user)

    for viewer in (early, second_user):
        await client.post(
            f"/v1/social/stories/{story['id']}/view", headers=_headers(viewer)
        )
    # Pin the timestamps rather than trusting two writes a millisecond apart.
    rows = (
        (
            await db_session.execute(
                select(SocialStoryView).where(
                    SocialStoryView.story_id == uuid.UUID(story["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.viewed_at = (
            now if row.viewer_id == second_user.id else now - timedelta(hours=1)
        )
    await db_session.commit()

    viewers = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        )
    )
    assert [v["viewer"]["username"] for v in viewers] == ["recent5", "early5"]
    assert all(v["viewed_at"] for v in viewers)


@pytest.mark.asyncio
async def test_a_story_nobody_watched_has_an_empty_viewer_list(client, created_user):
    await _claim(client, created_user, "teller6")
    story = await _post_story(client, created_user)

    assert (
        assert_envelope_ok(
            await client.get(
                f"/v1/social/stories/{story['id']}/viewers",
                headers=_headers(created_user),
            )
        )
        == []
    )


@pytest.mark.asyncio
async def test_the_viewer_list_survives_expiry_for_its_author(
    client, db_session, created_user, second_user
):
    """Unlike every other story read, this one is keyed off the row rather
    than the live predicate — an author who opens "who saw it" ten minutes
    after expiry still gets an answer, which is when they usually look."""
    await _claim(client, created_user, "teller7")
    await _claim(client, second_user, "watcher7")
    story = await _post_story(client, created_user)
    await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
    )
    await _expire(db_session, story["id"])

    viewers = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        )
    )
    assert [v["viewer"]["username"] for v in viewers] == ["watcher7"]


@pytest.mark.asyncio
async def test_a_deleted_story_has_no_viewer_list_at_all(
    client, created_user, second_user
):
    await _claim(client, created_user, "teller8")
    await _claim(client, second_user, "watcher8")
    story = await _post_story(client, created_user)
    await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
    )
    await client.delete(
        f"/v1/social/stories/{story['id']}", headers=_headers(created_user)
    )

    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_the_viewer_list_of_an_unknown_story_is_a_404(client, created_user):
    await _claim(client, created_user, "teller9")
    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{uuid.uuid4()}/viewers", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_watcher_with_no_claimed_handle_is_neither_counted_nor_named(
    client, db_session, created_user
):
    """`view_count` is the length of the viewer list, always.

    Marking seen needs no profile — the row is kept, so the view counts from
    the day that account claims a username — but until it has one there is
    nothing to name in the list, so it is not counted either. "1 view" above
    an empty list is a bug report, not a feature.
    """
    lurker = await make_user(db_session)
    await _claim(client, created_user, "teller10")
    story = await _post_story(client, created_user)

    resp = await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(lurker)
    )
    assert resp.status_code == 204

    viewers = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        )
    )
    assert viewers == []
    mine = assert_envelope_ok(
        await client.get("/v1/social/stories/teller10", headers=_headers(created_user))
    )
    assert mine[0]["view_count"] == 0

    # And once they claim one, the same stored view shows up on both sides.
    await _claim(client, lurker, "lurker10")
    viewers = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        )
    )
    assert [v["viewer"]["username"] for v in viewers] == ["lurker10"]
    mine = assert_envelope_ok(
        await client.get("/v1/social/stories/teller10", headers=_headers(created_user))
    )
    assert mine[0]["view_count"] == 1


# ── Story comments ──


@pytest.mark.asyncio
async def test_story_comments_read_oldest_first(client, created_user, second_user):
    """A story's replies are a conversation under one image; reversing them
    makes the answer arrive before the question."""
    await _claim(client, created_user, "teller11")
    await _claim(client, second_user, "chatter11")
    story = await _post_story(client, created_user)

    for body in ("first", "second", "third"):
        assert_envelope_ok(
            await client.post(
                f"/v1/social/stories/{story['id']}/comments",
                json={"body": body},
                headers=_headers(second_user),
            ),
            expected_status=201,
        )

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(second_user)
        )
    )
    assert [c["body"] for c in thread] == ["first", "second", "third"]
    # Their own comments, so they can take them back.
    assert all(c["can_delete"] for c in thread)
    assert {c["story_id"] for c in thread} == {story["id"]}


@pytest.mark.asyncio
async def test_a_bystander_cannot_delete_someone_elses_story_comment(
    client, db_session, created_user, second_user
):
    """Only its author, the story's author, or staff. A third party with the
    id must not be able to clear the thread."""
    bystander = await make_user(db_session)
    await _claim(client, created_user, "teller12")
    await _claim(client, second_user, "chatter12")
    await _claim(client, bystander, "bystander12")
    await client.post("/v1/social/users/teller12/follow", headers=_headers(bystander))
    story = await _post_story(client, created_user)
    made = assert_envelope_ok(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "not yours"},
            headers=_headers(second_user),
        ),
        expected_status=201,
    )

    seen_by_bystander = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(bystander)
        )
    )
    assert seen_by_bystander[0]["can_delete"] is False
    assert_envelope_error(
        await client.delete(
            f"/v1/social/stories/comments/{made['id']}", headers=_headers(bystander)
        ),
        expected_status=403,
    )


@pytest.mark.asyncio
async def test_an_empty_story_comment_is_refused(client, created_user):
    """Whitespace is not a comment — it renders as a blank row nobody can
    interpret, and the story's author gets pinged for it."""
    await _claim(client, created_user, "teller13")
    story = await _post_story(client, created_user)

    for body in ("", "   "):
        assert_envelope_error(
            await client.post(
                f"/v1/social/stories/{story['id']}/comments",
                json={"body": body},
                headers=_headers(created_user),
            ),
            expected_status=422,
        )


@pytest.mark.asyncio
async def test_an_overlong_story_comment_is_refused(client, created_user):
    await _claim(client, created_user, "teller14")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "x" * 501},
            headers=_headers(created_user),
        ),
        expected_status=422,
    )


@pytest.mark.asyncio
async def test_commenting_needs_a_claimed_handle(
    client, db_session, created_user, second_user
):
    """A comment draws a byline; an account with no username has nothing to
    put there, so the 409 sends them through onboarding first."""
    await _claim(client, created_user, "teller15")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "hi from nowhere"},
            headers=_headers(second_user),
        ),
        expected_status=409,
    )


@pytest.mark.asyncio
async def test_comments_on_an_unknown_story_are_a_404_either_way(client, created_user):
    await _claim(client, created_user, "teller16")
    missing = uuid.uuid4()
    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{missing}/comments", headers=_headers(created_user)
        ),
        expected_status=404,
    )
    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{missing}/comments",
            json={"body": "anyone there"},
            headers=_headers(created_user),
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_stranger_cannot_read_a_private_storys_comments(
    client, created_user, second_user
):
    await _claim(client, created_user, "hermit40", is_private=True)
    await _claim(client, second_user, "stranger40")
    story = await _post_story(client, created_user)
    await client.post(
        f"/v1/social/stories/{story['id']}/comments",
        json={"body": "just us"},
        headers=_headers(created_user),
    )

    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(second_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_comments_disappear_with_the_story_they_hang_off(
    client, db_session, created_user, second_user
):
    """Once a story expires there is nothing to read the replies against —
    they were about an image that is gone."""
    await _claim(client, created_user, "teller17")
    await _claim(client, second_user, "chatter17")
    story = await _post_story(client, created_user)
    await client.post(
        f"/v1/social/stories/{story['id']}/comments",
        json={"body": "nice"},
        headers=_headers(second_user),
    )
    await _expire(db_session, story["id"])

    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(created_user)
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_flagged_story_comment_is_filed_under_its_own_target_type(
    client, db_session, created_user, second_user, monkeypatch
):
    """A review case must name the thing it is about.

    A story comment is not a post: filed as ``post`` the case points at an id
    no post table holds, so the queue offers a dead link and resolving it
    removes nothing while stamping the case handled. It goes through the same
    chokepoint as everything else, as ``story_comment``, pointing at the row
    a moderator will actually act on.
    """
    from app.social import moderation
    from app.social.models import SocialModerationCase, SocialPost, SocialStoryComment

    async def _review(text=None, images=None):
        return moderation.Verdict(
            action=moderation.REVIEW, categories=["harassment"], score=0.8
        )

    monkeypatch.setattr(moderation, "screen", _review)

    await _claim(client, created_user, "teller18")
    await _claim(client, second_user, "chatter18")
    story = await _post_story(client, created_user)
    made = assert_envelope_ok(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "borderline"},
            headers=_headers(second_user),
        ),
        expected_status=201,
    )

    case = (
        (
            await db_session.execute(
                select(SocialModerationCase).where(
                    SocialModerationCase.target_id == uuid.UUID(made["id"])
                )
            )
        )
        .scalars()
        .one()
    )
    assert case.target_type == "story_comment"
    assert case.status == "open"
    # The target is a row that exists, and it is not a post.
    assert await db_session.get(SocialStoryComment, case.target_id) is not None
    assert await db_session.get(SocialPost, case.target_id) is None


@pytest.mark.asyncio
async def test_removing_a_story_comment_case_takes_the_comment_down(
    client, db_session, created_user, second_user, monkeypatch
):
    """Resolving with "remove" has to remove the thing. A case whose target
    type nothing can resolve leaves the flagged comment live under the story
    while the queue reports it handled — the one failure a review queue must
    never have.
    """
    from app.social import moderation
    from app.social.models import SocialModerationCase

    async def _review(text=None, images=None):
        return moderation.Verdict(
            action=moderation.REVIEW, categories=["harassment"], score=0.8
        )

    monkeypatch.setattr(moderation, "screen", _review)

    admin_user = await _make_admin(db_session)
    await _claim(client, created_user, "teller19")
    await _claim(client, second_user, "chatter19")
    story = await _post_story(client, created_user)
    made = assert_envelope_ok(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "borderline"},
            headers=_headers(second_user),
        ),
        expected_status=201,
    )
    case = (
        (
            await db_session.execute(
                select(SocialModerationCase).where(
                    SocialModerationCase.target_id == uuid.UUID(made["id"])
                )
            )
        )
        .scalars()
        .one()
    )

    resolved = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{case.id}/resolve",
            params={"action": "remove"},
            headers=_headers(admin_user),
        )
    )
    assert resolved["status"] == "removed"

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(created_user)
        )
    )
    assert thread == []


@pytest.mark.asyncio
async def test_removing_a_story_case_takes_the_story_down(
    client, db_session, created_user, monkeypatch
):
    """The same rule one level up: a flagged story is filed as ``story``, so
    the remove action reaches the story it names."""
    from app.social import moderation
    from app.social.models import SocialModerationCase

    async def _review(text=None, images=None):
        return moderation.Verdict(
            action=moderation.REVIEW, categories=["violence"], score=0.7
        )

    monkeypatch.setattr(moderation, "screen", _review)

    admin_user = await _make_admin(db_session)
    await _claim(client, created_user, "teller20")
    story = await _post_story(client, created_user, caption="borderline")

    case = (
        (
            await db_session.execute(
                select(SocialModerationCase).where(
                    SocialModerationCase.target_id == uuid.UUID(story["id"])
                )
            )
        )
        .scalars()
        .one()
    )
    assert case.target_type == "story"

    assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{case.id}/resolve",
            params={"action": "remove"},
            headers=_headers(admin_user),
        )
    )

    # Gone from its author's own tray — the only read that would still show
    # it if the removal had missed.
    assert_envelope_error(
        await client.get("/v1/social/stories/teller20", headers=_headers(created_user)),
        expected_status=404,
    )


# ── Auth boundary ──


@pytest.mark.asyncio
async def test_every_story_engagement_endpoint_requires_a_signed_in_caller(client):
    fake = uuid.uuid4()
    for method, path, kwargs in (
        ("post", f"/v1/social/stories/{fake}/view", {}),
        ("get", f"/v1/social/stories/{fake}/viewers", {}),
        ("get", f"/v1/social/stories/{fake}/comments", {}),
        ("post", f"/v1/social/stories/{fake}/comments", {"json": {"body": "hi"}}),
    ):
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path}"
