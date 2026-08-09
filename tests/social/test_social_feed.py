"""End-to-end tests for the community feed (``/v1/social`` feed half).

The properties worth defending, in order of how much a bug would hurt:

1. **Privacy** — a private account's posts must not leak into anyone's feed,
   tag page, permalink or trending counts.
2. **Feed definitions** — what "Following" and "For You" contain is a
   backend decision; if it drifts, two clients disagree about reality.
3. **Threading** — replies stay one level deep no matter what a client sends.
4. **Idempotence** — double-tapping a heart is one like, not two.
5. **Notifications** — the bell fires for other people's actions, never
   your own.
"""

from __future__ import annotations

import importlib.util
import io
import struct
import uuid
import zlib
from pathlib import Path

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.notification import Notification
from app.social.post_media import probe_size
from app.social.services.feed_common import (
    extract_hashtags,
    extract_mention_handles,
)
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card, make_user


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _claim(client, user, username: str, **extra) -> dict:
    resp = await client.put(
        "/v1/social/me",
        json={"username": username, **extra},
        headers=_headers(user),
    )
    return assert_envelope_ok(resp)


def _png(width: int = 8, height: int = 5) -> bytes:
    """A real, minimal PNG — so probe_size is exercised, not stubbed."""

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


async def _post(client, user, body: str | None = None, *, image: bool = False) -> dict:
    files = []
    if image:
        files.append(("images", ("shot.png", io.BytesIO(_png()), "image/png")))
    data = {}
    if body is not None:
        data["body"] = body
    resp = await client.post(
        "/v1/social/posts",
        data=data,
        files=files or None,
        headers=_headers(user),
    )
    return assert_envelope_ok(resp, expected_status=201)


async def _follow(client, follower, handle: str) -> dict:
    resp = await client.post(
        f"/v1/social/users/{handle}/follow", headers=_headers(follower)
    )
    return assert_envelope_ok(resp)


# ── Caption parsing (pure) ──


def test_hashtags_are_lowercased_deduped_and_bounded():
    tags = extract_hashtags("#Pokemon grail #pokemon #PSA10 end.#psa10 no#hash-dash")
    # Case-folded, first-appearance order, no duplicates.
    assert tags == ["pokemon", "psa10", "hash"]


def test_hashtag_does_not_swallow_trailing_punctuation():
    assert extract_hashtags("mint #charizard.") == ["charizard"]


def test_mentions_strip_trailing_dots():
    handles = extract_mention_handles("thanks @ash. and @misty_x, cc @ash")
    assert handles == ["ash", "misty_x"]


def test_probe_size_reads_png_dimensions():
    assert probe_size(_png(13, 7)) == (13, 7)


def test_probe_size_degrades_to_none_on_junk():
    assert probe_size(b"not an image at all") == (None, None)


# ── Posting ──


@pytest.mark.asyncio
async def test_post_requires_a_claimed_handle(client, created_user):
    resp = await client.post(
        "/v1/social/posts", data={"body": "hello"}, headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=409)


@pytest.mark.asyncio
async def test_empty_post_is_rejected(client, created_user):
    await _claim(client, created_user, "quiet")
    resp = await client.post(
        "/v1/social/posts", data={"body": "   "}, headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_post_with_image_roundtrips_through_the_media_endpoint(
    client, created_user
):
    await _claim(client, created_user, "shutterbug")
    post = await _post(client, created_user, "First pull #pokemon", image=True)

    assert len(post["media"]) == 1
    media = post["media"][0]
    # The aspect ratio ships with the payload so the client reserves space
    # before the bytes land.
    assert (media["width"], media["height"]) == (8, 5)

    resp = await client.get(media["url"].replace("/v1", "/v1"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert "immutable" in resp.headers["cache-control"]


@pytest.mark.asyncio
async def test_post_indexes_hashtags_and_resolves_mentions(
    client, db_session, created_user
):
    other = await make_user(db_session)
    await _claim(client, other, "mistyx")
    await _claim(client, created_user, "ashk")

    post = await _post(client, created_user, "trade with @mistyx #pokemon #psa10")
    assert set(post["hashtags"]) == {"pokemon", "psa10"}
    assert post["mentions"] == ["mistyx"]


@pytest.mark.asyncio
async def test_unknown_mention_is_not_indexed(client, created_user):
    await _claim(client, created_user, "ashk2")
    post = await _post(client, created_user, "hey @nobodyhere")
    assert post["mentions"] == []


@pytest.mark.asyncio
async def test_post_can_showcase_a_catalog_card(client, db_session, created_user):
    await _claim(client, created_user, "showcase")
    card = await make_card(db_session, name="Charizard")
    resp = await client.post(
        "/v1/social/posts",
        data={"body": "grail", "card_id": str(card.id)},
        headers=_headers(created_user),
    )
    post = assert_envelope_ok(resp, expected_status=201)
    assert post["card"]["card_id"] == str(card.id)
    assert post["card"]["name"] == "Charizard"


@pytest.mark.asyncio
async def test_author_can_delete_and_a_stranger_cannot(
    client, db_session, created_user
):
    stranger = await make_user(db_session)
    await _claim(client, stranger, "stranger")
    await _claim(client, created_user, "owner")
    post = await _post(client, created_user, "mine")

    denied = await client.delete(
        f"/v1/social/posts/{post['id']}", headers=_headers(stranger)
    )
    assert_envelope_error(denied, expected_status=403)

    ok = await client.delete(
        f"/v1/social/posts/{post['id']}", headers=_headers(created_user)
    )
    assert ok.status_code == 204

    # Soft-deleted posts vanish from every read surface.
    gone = await client.get(
        f"/v1/social/posts/{post['id']}", headers=_headers(created_user)
    )
    assert_envelope_error(gone, expected_status=404)


# ── Feed definitions ──


@pytest.mark.asyncio
async def test_following_feed_has_people_you_follow_and_yourself(
    client, db_session, created_user
):
    followed = await make_user(db_session)
    ignored = await make_user(db_session)
    await _claim(client, followed, "followed")
    await _claim(client, ignored, "ignored")
    await _claim(client, created_user, "viewer")

    await _post(client, followed, "from someone I follow")
    await _post(client, ignored, "from a stranger")
    await _post(client, created_user, "my own post")
    await _follow(client, created_user, "followed")

    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed",
            params={"tab": "following"},
            headers=_headers(created_user),
        )
    )
    bodies = {item["body"] for item in feed["items"]}
    assert bodies == {"from someone I follow", "my own post"}


@pytest.mark.asyncio
async def test_mine_feed_is_only_my_posts(client, db_session, created_user):
    other = await make_user(db_session)
    await _claim(client, other, "other1")
    await _claim(client, created_user, "me1")
    await _post(client, other, "theirs")
    await _post(client, created_user, "mine")

    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "mine"}, headers=_headers(created_user)
        )
    )
    assert [i["body"] for i in feed["items"]] == ["mine"]


@pytest.mark.asyncio
async def test_foryou_excludes_your_own_posts(client, db_session, created_user):
    other = await make_user(db_session)
    await _claim(client, other, "other2")
    await _claim(client, created_user, "me2")
    await _post(client, other, "discoverable")
    await _post(client, created_user, "my own")

    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "foryou"}, headers=_headers(created_user)
        )
    )
    bodies = [i["body"] for i in feed["items"]]
    assert "discoverable" in bodies
    assert "my own" not in bodies


@pytest.mark.asyncio
async def test_unknown_tab_is_rejected_rather_than_silently_defaulted(
    client, created_user
):
    await _claim(client, created_user, "tabby")
    resp = await client.get(
        "/v1/social/feed", params={"tab": "explore"}, headers=_headers(created_user)
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_feed_pages_with_a_cursor_and_never_repeats_a_post(
    client, db_session, created_user
):
    await _claim(client, created_user, "pager")
    for n in range(5):
        await _post(client, created_user, f"post {n}")

    first = assert_envelope_ok(
        await client.get(
            "/v1/social/feed",
            params={"tab": "mine", "limit": 2},
            headers=_headers(created_user),
        )
    )
    assert len(first["items"]) == 2
    assert first["next_cursor"]

    second = assert_envelope_ok(
        await client.get(
            "/v1/social/feed",
            params={"tab": "mine", "limit": 2, "cursor": first["next_cursor"]},
            headers=_headers(created_user),
        )
    )
    ids = {i["id"] for i in first["items"]} & {i["id"] for i in second["items"]}
    assert ids == set()


@pytest.mark.asyncio
async def test_a_cursor_we_did_not_issue_is_rejected(client, created_user):
    await _claim(client, created_user, "forger")
    resp = await client.get(
        "/v1/social/feed",
        params={"tab": "mine", "cursor": "!!!not-a-cursor!!!"},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=400)


# ── Privacy ──


@pytest.mark.asyncio
async def test_private_accounts_posts_are_invisible_until_the_follow_is_accepted(
    client, db_session, created_user
):
    private = await make_user(db_session)
    await _claim(client, private, "vaulted", is_private=True)
    await _claim(client, created_user, "onlooker")
    post = await _post(client, private, "my secret grail #rare")

    # Not in discovery…
    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "foryou"}, headers=_headers(created_user)
        )
    )
    assert post["id"] not in {i["id"] for i in feed["items"]}

    # …not on their profile…
    profile_feed = assert_envelope_ok(
        await client.get(
            "/v1/social/users/vaulted/posts", headers=_headers(created_user)
        )
    )
    assert profile_feed["items"] == []

    # …not by permalink, and 404 rather than 403 so the response doesn't
    # confirm the post exists…
    assert_envelope_error(
        await client.get(
            f"/v1/social/posts/{post['id']}", headers=_headers(created_user)
        ),
        expected_status=404,
    )

    # …and not behind its hashtag.
    tagged = assert_envelope_ok(
        await client.get(
            "/v1/social/hashtags/rare/posts", headers=_headers(created_user)
        )
    )
    assert tagged["items"] == []

    # Accepted follower sees everything.
    state = await _follow(client, created_user, "vaulted")
    assert state["relationship"] == "requested"
    requests = assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(private))
    )
    accept = await client.post(
        f"/v1/social/requests/{requests[0]['id']}/accept", headers=_headers(private)
    )
    assert accept.status_code == 204

    now = assert_envelope_ok(
        await client.get(
            "/v1/social/users/vaulted/posts", headers=_headers(created_user)
        )
    )
    assert [i["id"] for i in now["items"]] == [post["id"]]


@pytest.mark.asyncio
async def test_a_private_accounts_tags_do_not_inflate_trending(
    client, db_session, created_user
):
    private = await make_user(db_session)
    await _claim(client, private, "hidden", is_private=True)
    await _claim(client, created_user, "watcher")
    await _post(client, private, "#unseen #unseen")

    trending = assert_envelope_ok(
        await client.get("/v1/social/hashtags/trending", headers=_headers(created_user))
    )
    assert "unseen" not in {t["tag"] for t in trending}


@pytest.mark.asyncio
async def test_stranger_cannot_like_or_comment_on_a_private_post(
    client, db_session, created_user
):
    private = await make_user(db_session)
    await _claim(client, private, "sealed", is_private=True)
    await _claim(client, created_user, "outsider")
    post = await _post(client, private, "locked")

    assert_envelope_error(
        await client.post(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        ),
        expected_status=404,
    )
    assert_envelope_error(
        await client.post(
            f"/v1/social/posts/{post['id']}/comments",
            json={"body": "nice"},
            headers=_headers(created_user),
        ),
        expected_status=404,
    )


# ── Likes ──


@pytest.mark.asyncio
async def test_liking_twice_is_still_one_like(client, db_session, created_user):
    author = await make_user(db_session)
    await _claim(client, author, "author1")
    await _claim(client, created_user, "liker1")
    post = await _post(client, author, "double tap me")

    first = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    second = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    assert first["like_count"] == second["like_count"] == 1
    assert second["liked"] is True

    off = assert_envelope_ok(
        await client.delete(
            f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
        )
    )
    assert off == {"liked": False, "like_count": 0}


@pytest.mark.asyncio
async def test_like_state_is_per_viewer(client, db_session, created_user):
    author = await make_user(db_session)
    await _claim(client, author, "author2")
    await _claim(client, created_user, "liker2")
    post = await _post(client, author, "counted once")
    await client.post(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )

    mine = assert_envelope_ok(
        await client.get(
            f"/v1/social/posts/{post['id']}", headers=_headers(created_user)
        )
    )
    theirs = assert_envelope_ok(
        await client.get(f"/v1/social/posts/{post['id']}", headers=_headers(author))
    )
    assert mine["viewer_has_liked"] is True
    assert theirs["viewer_has_liked"] is False
    assert mine["like_count"] == theirs["like_count"] == 1


# ── Comments ──


@pytest.mark.asyncio
async def test_comment_thread_reads_oldest_first_with_inline_replies(
    client, db_session, created_user
):
    author = await make_user(db_session)
    await _claim(client, author, "author3")
    await _claim(client, created_user, "talker")
    post = await _post(client, author, "thread me")
    pid = post["id"]

    first = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "first"},
            headers=_headers(created_user),
        ),
        expected_status=201,
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "second"},
            headers=_headers(author),
        ),
        expected_status=201,
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "a reply", "parent_id": first["id"]},
            headers=_headers(author),
        ),
        expected_status=201,
    )

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/posts/{pid}/comments", headers=_headers(created_user)
        )
    )
    # Top-level only, oldest first; the reply rides along with its parent.
    assert [c["body"] for c in thread["items"]] == ["first", "second"]
    assert thread["items"][0]["reply_count"] == 1
    assert [r["body"] for r in thread["items"][0]["replies"]] == ["a reply"]
    # The bubble count includes replies.
    assert thread["total"] == 3


@pytest.mark.asyncio
async def test_replying_to_a_reply_flattens_onto_the_top_level_comment(
    client, db_session, created_user
):
    await _claim(client, created_user, "flattener")
    post = await _post(client, created_user, "one level only")
    pid = post["id"]

    top = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "top"},
            headers=_headers(created_user),
        ),
        expected_status=201,
    )
    reply = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "reply", "parent_id": top["id"]},
            headers=_headers(created_user),
        ),
        expected_status=201,
    )
    nested = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{pid}/comments",
            json={"body": "reply to the reply", "parent_id": reply["id"]},
            headers=_headers(created_user),
        ),
        expected_status=201,
    )
    # Attached to the TOP-level comment, not to the reply it answered.
    assert nested["parent_id"] == top["id"]


@pytest.mark.asyncio
async def test_comments_have_their_own_likes(client, db_session, created_user):
    author = await make_user(db_session)
    await _claim(client, author, "author4")
    await _claim(client, created_user, "hearter")
    post = await _post(client, author, "post")
    comment = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post['id']}/comments",
            json={"body": "great pull"},
            headers=_headers(author),
        ),
        expected_status=201,
    )

    liked = assert_envelope_ok(
        await client.post(
            f"/v1/social/comments/{comment['id']}/like", headers=_headers(created_user)
        )
    )
    assert liked == {"liked": True, "like_count": 1}

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/posts/{post['id']}/comments", headers=_headers(created_user)
        )
    )
    assert thread["items"][0]["viewer_has_liked"] is True
    # The POST's like count is untouched by a comment like.
    assert thread["items"][0]["like_count"] == 1


@pytest.mark.asyncio
async def test_post_author_can_remove_someone_elses_comment(
    client, db_session, created_user
):
    commenter = await make_user(db_session)
    await _claim(client, commenter, "commenter")
    await _claim(client, created_user, "postowner")
    post = await _post(client, created_user, "my post")
    comment = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post['id']}/comments",
            json={"body": "spam"},
            headers=_headers(commenter),
        ),
        expected_status=201,
    )

    resp = await client.delete(
        f"/v1/social/comments/{comment['id']}", headers=_headers(created_user)
    )
    assert resp.status_code == 204

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/posts/{post['id']}/comments", headers=_headers(created_user)
        )
    )
    assert thread["items"] == []


# ── Hashtags + search ──


@pytest.mark.asyncio
async def test_tag_feed_and_trending_count_public_posts(
    client, db_session, created_user
):
    await _claim(client, created_user, "tagger")
    await _post(client, created_user, "#pokemon one")
    await _post(client, created_user, "#pokemon two")
    await _post(client, created_user, "#magic once")

    trending = assert_envelope_ok(
        await client.get("/v1/social/hashtags/trending", headers=_headers(created_user))
    )
    counts = {t["tag"]: t["post_count"] for t in trending}
    assert counts["pokemon"] == 2
    assert counts["magic"] == 1
    # Most-used first — this list is a ranked chip row, not a set.
    assert trending[0]["tag"] == "pokemon"

    tagged = assert_envelope_ok(
        await client.get(
            "/v1/social/hashtags/pokemon/posts", headers=_headers(created_user)
        )
    )
    assert len(tagged["items"]) == 2


@pytest.mark.asyncio
async def test_tag_lookup_accepts_a_leading_hash_and_any_case(client, created_user):
    await _claim(client, created_user, "caser")
    await _post(client, created_user, "#Charizard")
    for spelling in ("charizard", "Charizard", "%23charizard"):
        tagged = assert_envelope_ok(
            await client.get(
                f"/v1/social/hashtags/{spelling}/posts", headers=_headers(created_user)
            )
        )
        assert len(tagged["items"]) == 1, spelling


@pytest.mark.asyncio
async def test_search_all_returns_people_and_tags_together(
    client, db_session, created_user
):
    other = await make_user(db_session)
    await _claim(client, other, "pokefan")
    await _claim(client, created_user, "searcher")
    await _post(client, other, "#pokemon")

    found = assert_envelope_ok(
        await client.get(
            "/v1/social/search/all",
            params={"q": "poke"},
            headers=_headers(created_user),
        )
    )
    assert "pokefan" in {u["username"] for u in found["users"]}
    assert "pokemon" in {h["tag"] for h in found["hashtags"]}


# ── Notifications ──


@pytest.mark.asyncio
async def test_a_like_notifies_the_author_once(client, db_session, created_user):
    author = await make_user(db_session)
    await _claim(client, author, "author5")
    await _claim(client, created_user, "fan")
    post = await _post(client, author, "notify me")

    await client.post(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )
    await client.delete(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )
    await client.post(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == author.id,
                    Notification.kind == "social_post_like",
                )
            )
        )
        .scalars()
        .all()
    )
    # Like/unlike/like is not a way to ping someone three times.
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_liking_your_own_post_notifies_nobody(client, created_user, db_session):
    await _claim(client, created_user, "selfliker")
    post = await _post(client, created_user, "mine")
    await client.post(
        f"/v1/social/posts/{post['id']}/like", headers=_headers(created_user)
    )

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == created_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_a_reply_notifies_the_replied_to_and_the_post_author_once_each(
    client, db_session, created_user
):
    author = await make_user(db_session)
    commenter = await make_user(db_session)
    await _claim(client, author, "author6")
    await _claim(client, commenter, "commenter6")
    await _claim(client, created_user, "replier6")
    post = await _post(client, author, "thread")
    comment = assert_envelope_ok(
        await client.post(
            f"/v1/social/posts/{post['id']}/comments",
            json={"body": "first"},
            headers=_headers(commenter),
        ),
        expected_status=201,
    )
    await client.post(
        f"/v1/social/posts/{post['id']}/comments",
        json={"body": "answering", "parent_id": comment["id"]},
        headers=_headers(created_user),
    )

    kinds = {
        (row.user_id, row.kind)
        for row in (await db_session.execute(select(Notification))).scalars().all()
    }
    assert (commenter.id, "social_comment_reply") in kinds
    assert (author.id, "social_post_comment") in kinds
    assert not any(user_id == created_user.id for user_id, _ in kinds)


@pytest.mark.asyncio
async def test_a_mention_notifies_the_person_named(client, db_session, created_user):
    named = await make_user(db_session)
    await _claim(client, named, "namedone")
    await _claim(client, created_user, "mentioner")
    await _post(client, created_user, "look at this @namedone")

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == named.id,
                    Notification.kind == "social_mention",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].href.startswith("/app/community/p/")


@pytest.mark.asyncio
async def test_following_someone_notifies_them(client, db_session, created_user):
    target = await make_user(db_session)
    await _claim(client, target, "followed6")
    await _claim(client, created_user, "follower6")
    await _follow(client, created_user, "followed6")

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == target.id,
                    Notification.kind == "social_follow",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


# ── Guard rails ──


@pytest.mark.asyncio
async def test_post_media_404s_for_an_id_that_was_never_uploaded(client, created_user):
    resp = await client.get(f"/v1/social/posts/media/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_image_uploads_are_refused(client, created_user):
    await _claim(client, created_user, "uploader")
    resp = await client.post(
        "/v1/social/posts",
        data={"body": "payload"},
        files=[("images", ("evil.svg", io.BytesIO(b"<svg/>"), "image/svg+xml"))],
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=415)


# ── Migration ↔ ORM agreement ──


@pytest.mark.parametrize(
    ("filename", "tables"),
    [
        (
            "0050_social_feed.py",
            [
                "social_posts",
                "social_post_media",
                "social_post_likes",
                "social_post_comments",
                "social_comment_likes",
                "social_post_hashtags",
                "social_post_mentions",
            ],
        ),
        ("0051_social_moderation.py", ["social_moderation_cases"]),
    ],
)
def test_social_migrations_create_exactly_what_the_models_declare(filename, tables):
    """The feed's migration and its models must not drift apart.

    This is the shape of the worst outage this backend has had: a column
    added to a model without a matching migration, so every query naming it
    500s in production while passing locally against a metadata-built test
    schema. Here the migration is executed for real and the result compared
    to the ORM, column for column and index for index.
    """
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.db import Base

    # Loaded by path: the versions directory is not an importable package
    # (alembic loads revisions by file), so there is no module name for it.
    spec = importlib.util.spec_from_file_location(
        f"migration_{filename}",
        Path(__file__).resolve().parents[2] / "app/db/alembic/versions" / filename,
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        # The FK targets the migration expects to already exist.
        Base.metadata.create_all(
            conn,
            tables=[
                Base.metadata.tables["users"],
                Base.metadata.tables["card_sets"],
                Base.metadata.tables["cards"],
            ],
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

        inspector = sa.inspect(conn)
        for name in tables:
            actual = {c["name"] for c in inspector.get_columns(name)}
            expected = set(Base.metadata.tables[name].columns.keys())
            assert actual == expected, f"{name}: migration/ORM column drift"

            actual_ix = {ix["name"] for ix in inspector.get_indexes(name)}
            expected_ix = {
                ix.name for ix in Base.metadata.tables[name].indexes if ix.name
            }
            missing = expected_ix - actual_ix
            assert not missing, f"{name}: migration is missing index(es) {missing}"


# ── "Someone you follow posted" ──


@pytest.mark.asyncio
async def test_posting_notifies_your_followers_with_a_readable_summary(
    client, db_session, created_user
):
    """The line has to say something. "New post" tells you nothing about
    whether to open it, so the summary is the caption's opening words."""
    follower = await make_user(db_session)
    stranger = await make_user(db_session)
    await _claim(client, created_user, "author20")
    await _claim(client, follower, "follower20")
    await _claim(client, stranger, "stranger20")
    await _follow(client, follower, "author20")

    await _post(client, created_user, "Pulled the Umbreon alt art #pokemon")

    rows = (
        (
            await db_session.execute(
                select(Notification).where(Notification.kind == "social_new_post")
            )
        )
        .scalars()
        .all()
    )
    # The follower, and only the follower.
    assert [r.user_id for r in rows] == [follower.id]
    assert rows[0].body == "Pulled the Umbreon alt art #pokemon"
    # …and it opens the post it's about.
    assert rows[0].href.endswith(str(rows[0].data["post_id"]))


@pytest.mark.asyncio
async def test_a_captionless_post_still_says_what_it_is(
    client, db_session, created_user
):
    follower = await make_user(db_session)
    await _claim(client, created_user, "author21")
    await _claim(client, follower, "follower21")
    await _follow(client, follower, "author21")

    await _post(client, created_user, None, image=True)

    row = (
        (
            await db_session.execute(
                select(Notification).where(Notification.kind == "social_new_post")
            )
        )
        .scalars()
        .one()
    )
    assert row.body == "Shared a photo"


@pytest.mark.asyncio
async def test_you_are_not_notified_about_your_own_post(
    client, db_session, created_user
):
    other = await make_user(db_session)
    await _claim(client, created_user, "author22")
    await _claim(client, other, "follower22")
    # created_user follows `other`, then posts themselves.
    await _follow(client, created_user, "follower22")
    await _post(client, created_user, "mine")

    rows = (
        (
            await db_session.execute(
                select(Notification).where(
                    Notification.user_id == created_user.id,
                    Notification.kind == "social_new_post",
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


# ── The hashtag page's Top / Recent split ──


@pytest.mark.asyncio
async def test_a_tag_page_leads_with_the_best_of_the_tag_not_the_newest(
    client, db_session, created_user
):
    """Arriving on #pokemon and seeing whatever was posted ninety seconds
    ago tells you nothing about the tag. `top` is the page's default."""
    fans = [await make_user(db_session) for _ in range(3)]
    for i, fan in enumerate(fans):
        await _claim(client, fan, f"fan{i}30")
    await _claim(client, created_user, "tagtop")

    quiet = await _post(client, created_user, "quiet one #pokemon")
    loved = await _post(client, created_user, "the good one #pokemon")
    newest = await _post(client, created_user, "just now #pokemon")

    for fan in fans:
        await client.post(f"/v1/social/posts/{loved['id']}/like", headers=_headers(fan))

    top = assert_envelope_ok(
        await client.get(
            "/v1/social/hashtags/pokemon/posts",
            params={"sort": "top"},
            headers=_headers(created_user),
        )
    )
    assert top["items"][0]["id"] == loved["id"]

    recent = assert_envelope_ok(
        await client.get(
            "/v1/social/hashtags/pokemon/posts",
            params={"sort": "recent"},
            headers=_headers(created_user),
        )
    )
    assert recent["items"][0]["id"] == newest["id"]
    assert quiet["id"] in {i["id"] for i in recent["items"]}


@pytest.mark.asyncio
async def test_top_is_the_default_for_a_tag_page(client, db_session, created_user):
    fan = await make_user(db_session)
    await _claim(client, fan, "fandefault")
    await _claim(client, created_user, "tagdefault")

    await _post(client, created_user, "first #psa10")
    loved = await _post(client, created_user, "loved #psa10")
    await _post(client, created_user, "newest #psa10")
    await client.post(f"/v1/social/posts/{loved['id']}/like", headers=_headers(fan))

    page = assert_envelope_ok(
        await client.get(
            "/v1/social/hashtags/psa10/posts", headers=_headers(created_user)
        )
    )
    assert page["items"][0]["id"] == loved["id"]
