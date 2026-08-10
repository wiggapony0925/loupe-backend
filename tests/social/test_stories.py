"""Stories: the tray, expiry, privacy, views and comments.

The behaviours worth pinning are the ones that are wrong in a way nobody
notices until it matters — a story that outlives its 24 hours, a private
account's story in a stranger's tray, or a viewer list someone else can read.
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
from app.social.models import SocialStory
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


def _png(width: int = 12, height: int = 20) -> bytes:
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


async def _post_story(client, user, *, caption: str | None = None, expect: int = 201):
    data = {}
    if caption is not None:
        data["caption"] = caption
    resp = await client.post(
        "/v1/social/stories",
        data=data,
        files={"media": ("story.png", io.BytesIO(_png()), "image/png")},
        headers=_headers(user),
    )
    if expect != 201:
        return assert_envelope_error(resp, expected_status=expect)
    return assert_envelope_ok(resp, expected_status=201)


# ── Posting + expiry ──


@pytest.mark.asyncio
async def test_posting_a_story_sets_a_24h_expiry(client, created_user):
    await _claim(client, created_user, "storyteller")
    story = await _post_story(client, created_user, caption="fresh pulls")

    assert story["caption"] == "fresh pulls"
    assert story["kind"] == "image"
    # The dimensions the client needs to frame it before the bytes land.
    assert (story["width"], story["height"]) == (12, 20)

    created = datetime.fromisoformat(story["created_at"])
    expires = datetime.fromisoformat(story["expires_at"])
    assert timedelta(hours=23, minutes=59) < expires - created <= timedelta(hours=24)


@pytest.mark.asyncio
async def test_an_expired_story_vanishes_from_every_live_read(
    client, db_session, created_user, second_user
):
    """Expiry is a PREDICATE, not a sweeper — so it takes effect on the
    clock, not on whenever a cron next happens to run."""
    await _claim(client, created_user, "expiring")
    await _claim(client, second_user, "watcher")
    story = await _post_story(client, created_user)

    # Reach into the row and age it out, the way the clock would.
    row = (
        await db_session.execute(
            select(SocialStory).where(SocialStory.id == uuid.UUID(story["id"]))
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert tray["entries"] == []
    assert_envelope_error(
        await client.get("/v1/social/stories/expiring", headers=_headers(second_user)),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_the_author_still_finds_it_in_their_archive(
    client, db_session, created_user
):
    """The archive is the one read that ignores expiry — and only ever for
    the person who posted it."""
    await _claim(client, created_user, "archivist")
    story = await _post_story(client, created_user, caption="yesterday")

    row = (
        await db_session.execute(
            select(SocialStory).where(SocialStory.id == uuid.UUID(story["id"]))
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()

    archive = assert_envelope_ok(
        await client.get("/v1/social/stories/archive", headers=_headers(created_user))
    )
    assert [s["caption"] for s in archive] == ["yesterday"]


@pytest.mark.asyncio
async def test_the_archive_is_only_ever_your_own(client, created_user, second_user):
    await _claim(client, created_user, "mine1")
    await _claim(client, second_user, "theirs1")
    await _post_story(client, second_user, caption="not yours")

    archive = assert_envelope_ok(
        await client.get("/v1/social/stories/archive", headers=_headers(created_user))
    )
    assert archive == []


# ── Privacy ──


@pytest.mark.asyncio
async def test_a_private_accounts_story_is_invisible_to_a_stranger(
    client, created_user, second_user
):
    await _claim(client, created_user, "hermit1", is_private=True)
    await _claim(client, second_user, "stranger1")
    await _post_story(client, created_user)

    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert tray["entries"] == []
    # 404, not 403: whether a private account has a story going is itself
    # information, and this endpoint shouldn't confirm it.
    assert_envelope_error(
        await client.get("/v1/social/stories/hermit1", headers=_headers(second_user)),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_an_accepted_follower_sees_a_private_story(
    client, created_user, second_user
):
    await _claim(client, created_user, "hermit2", is_private=True)
    await _claim(client, second_user, "fan2")
    await client.post("/v1/social/users/hermit2/follow", headers=_headers(second_user))
    reqs = assert_envelope_ok(
        await client.get("/v1/social/requests", headers=_headers(created_user))
    )
    await client.post(
        f"/v1/social/requests/{reqs[0]['id']}/accept", headers=_headers(created_user)
    )
    await _post_story(client, created_user, caption="for my followers")

    got = assert_envelope_ok(
        await client.get("/v1/social/stories/hermit2", headers=_headers(second_user))
    )
    assert [s["caption"] for s in got] == ["for my followers"]


# ── The tray ──


@pytest.mark.asyncio
async def test_the_tray_puts_unseen_first_and_separates_your_own(
    client, created_user, second_user, db_session
):
    """Unseen-first is what makes a tray a queue rather than a leaderboard."""
    seen_author = await make_user(db_session)
    await _claim(client, created_user, "me3")
    await _claim(client, second_user, "unseen3")
    await _claim(client, seen_author, "seen3")

    for handle_user in (second_user, seen_author):
        await client.post(
            f"/v1/social/users/{'unseen3' if handle_user is second_user else 'seen3'}/follow",
            headers=_headers(created_user),
        )
    await _post_story(client, created_user)
    already = await _post_story(client, seen_author)
    await _post_story(client, second_user)

    await client.post(
        f"/v1/social/stories/{already['id']}/view", headers=_headers(created_user)
    )

    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(created_user))
    )
    # Your own is its own slot — it renders as "Your story", not as a queue
    # item, and its ring is never lit.
    assert tray["mine"]["author"]["username"] == "me3"
    assert tray["mine"]["has_unseen"] is False
    assert [e["author"]["username"] for e in tray["entries"]] == ["unseen3", "seen3"]
    assert [e["has_unseen"] for e in tray["entries"]] == [True, False]


# ── Views ──


@pytest.mark.asyncio
async def test_views_are_unique_and_never_count_the_author(
    client, created_user, second_user
):
    await _claim(client, created_user, "poster4")
    await _claim(client, second_user, "viewer4")
    story = await _post_story(client, created_user)

    # The author opening their own story must not appear in their own list.
    await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(created_user)
    )
    # A re-watch counts people, not plays.
    for _ in range(3):
        await client.post(
            f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
        )

    viewers = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(created_user)
        )
    )
    assert [v["viewer"]["username"] for v in viewers] == ["viewer4"]


@pytest.mark.asyncio
async def test_nobody_reads_someone_elses_viewer_list(
    client, created_user, second_user
):
    await _claim(client, created_user, "poster5")
    await _claim(client, second_user, "nosy5")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.get(
            f"/v1/social/stories/{story['id']}/viewers", headers=_headers(second_user)
        ),
        expected_status=403,
    )


@pytest.mark.asyncio
async def test_view_counts_are_hidden_from_everyone_but_the_author(
    client, created_user, second_user
):
    await _claim(client, created_user, "poster6")
    await _claim(client, second_user, "viewer6")
    story = await _post_story(client, created_user)
    await client.post(
        f"/v1/social/stories/{story['id']}/view", headers=_headers(second_user)
    )

    theirs = assert_envelope_ok(
        await client.get("/v1/social/stories/poster6", headers=_headers(second_user))
    )
    assert theirs[0]["view_count"] == 0

    ours = assert_envelope_ok(
        await client.get("/v1/social/stories/poster6", headers=_headers(created_user))
    )
    assert ours[0]["view_count"] == 1


# ── Comments ──


@pytest.mark.asyncio
async def test_comment_on_a_story_and_read_it_back(client, created_user, second_user):
    await _claim(client, created_user, "poster7")
    await _claim(client, second_user, "commenter7")
    story = await _post_story(client, created_user)

    made = assert_envelope_ok(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "insane pull"},
            headers=_headers(second_user),
        ),
        expected_status=201,
    )
    assert made["body"] == "insane pull"

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(created_user)
        )
    )
    assert [c["body"] for c in thread] == ["insane pull"]
    # It's their story, so they can clear anything under it.
    assert thread[0]["can_delete"] is True


@pytest.mark.asyncio
async def test_the_story_author_can_remove_a_comment_they_did_not_write(
    client, created_user, second_user
):
    await _claim(client, created_user, "poster8")
    await _claim(client, second_user, "commenter8")
    story = await _post_story(client, created_user)
    made = assert_envelope_ok(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "rude thing"},
            headers=_headers(second_user),
        ),
        expected_status=201,
    )

    resp = await client.delete(
        f"/v1/social/stories/comments/{made['id']}", headers=_headers(created_user)
    )
    assert resp.status_code == 204

    thread = assert_envelope_ok(
        await client.get(
            f"/v1/social/stories/{story['id']}/comments", headers=_headers(created_user)
        )
    )
    assert thread == []


@pytest.mark.asyncio
async def test_you_cannot_comment_on_a_story_you_cannot_see(
    client, created_user, second_user
):
    await _claim(client, created_user, "hermit9", is_private=True)
    await _claim(client, second_user, "stranger9")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.post(
            f"/v1/social/stories/{story['id']}/comments",
            json={"body": "let me in"},
            headers=_headers(second_user),
        ),
        expected_status=404,
    )


# ── Lifecycle ──


@pytest.mark.asyncio
async def test_only_the_author_or_staff_deletes_a_story(
    client, created_user, second_user
):
    await _claim(client, created_user, "poster10")
    await _claim(client, second_user, "stranger10")
    story = await _post_story(client, created_user)

    assert_envelope_error(
        await client.delete(
            f"/v1/social/stories/{story['id']}", headers=_headers(second_user)
        ),
        expected_status=403,
    )
    resp = await client.delete(
        f"/v1/social/stories/{story['id']}", headers=_headers(created_user)
    )
    assert resp.status_code == 204

    # Gone from the archive too — a delete is not an expiry.
    archive = assert_envelope_ok(
        await client.get("/v1/social/stories/archive", headers=_headers(created_user))
    )
    assert archive == []


@pytest.mark.asyncio
async def test_a_story_needs_a_claimed_handle(client, created_user):
    await _post_story(client, created_user, expect=409)


@pytest.mark.asyncio
async def test_the_media_endpoint_serves_the_bytes_back(client, created_user):
    await _claim(client, created_user, "poster11")
    story = await _post_story(client, created_user)
    resp = await client.get(f"/v1/social/stories/media/{story['id']}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == _png()


# ── Video ──


def _mp4(width: int = 1080, height: int = 1920, version: int = 0) -> bytes:
    """A synthetic MP4 carrying just enough for the dimension probe.

    Real files are a tree of boxes; the probe only needs to find a `tkhd`
    and read the fixed-point display size off its tail, so this builds
    exactly that — plus a leading audio-style tkhd of zeroes, to prove the
    probe skips a track with no size rather than reporting 0x0.
    """

    def tkhd(w: int, h: int, ver: int) -> bytes:
        body = bytes([ver]) + b"\x00\x00\x00"  # version + flags
        body += b"\x00" * (20 if ver == 0 else 32)  # times, id, duration
        body += b"\x00" * 16  # reserved, layer, group, volume
        body += b"\x00" * 36  # matrix
        body += struct.pack(">II", w << 16, h << 16)  # 16.16 fixed point
        return struct.pack(">I", len(body) + 8) + b"tkhd" + body

    ftyp = struct.pack(">I", 20) + b"ftypisom" + b"\x00" * 8
    return ftyp + tkhd(0, 0, 0) + tkhd(width, height, version)


@pytest.mark.parametrize("version", [0, 1])
def test_mp4_dimensions_are_read_from_the_track_header(version):
    """Without these two numbers a full-screen story opens as a square and
    snaps to shape when the first frame decodes."""
    from app.social.post_media import probe_size

    assert probe_size(_mp4(1080, 1920, version)) == (1080, 1920)


def test_an_unreadable_file_degrades_to_unknown_rather_than_raising():
    from app.social.post_media import probe_size

    assert probe_size(b"\x00\x00\x00\x18ftypmp42" + b"\xff" * 40) == (None, None)
    assert probe_size(b"not a media file at all") == (None, None)


@pytest.mark.asyncio
async def test_a_video_story_keeps_its_duration_and_reports_its_kind(
    client, created_user
):
    await _claim(client, created_user, "videographer")
    resp = await client.post(
        "/v1/social/stories",
        data={"duration_ms": "8000"},
        files={"media": ("clip.mp4", io.BytesIO(_mp4()), "video/mp4")},
        headers=_headers(created_user),
    )
    story = assert_envelope_ok(resp, expected_status=201)
    assert story["kind"] == "video"
    assert story["duration_ms"] == 8000
    assert (story["width"], story["height"]) == (1080, 1920)


@pytest.mark.asyncio
async def test_a_still_never_carries_a_duration(client, created_user):
    """A number here would make the progress bar honour it, and a photo has
    no length to honour."""
    await _claim(client, created_user, "stiller")
    resp = await client.post(
        "/v1/social/stories",
        data={"duration_ms": "9000"},
        files={"media": ("s.png", io.BytesIO(_png()), "image/png")},
        headers=_headers(created_user),
    )
    assert assert_envelope_ok(resp, expected_status=201)["duration_ms"] is None


@pytest.mark.asyncio
async def test_an_overlong_video_is_refused(client, created_user):
    await _claim(client, created_user, "rambler")
    resp = await client.post(
        "/v1/social/stories",
        data={"duration_ms": "60000"},
        files={"media": ("long.mp4", io.BytesIO(_mp4()), "video/mp4")},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_an_unsupported_type_is_refused(client, created_user):
    """No WebM: iOS can't play it, and accepting an upload half the users
    can't watch is worse than refusing it."""
    await _claim(client, created_user, "webmfan")
    resp = await client.post(
        "/v1/social/stories",
        files={"media": ("x.webm", io.BytesIO(b"\x1a\x45\xdf\xa3"), "video/webm")},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=415)


@pytest.mark.asyncio
async def test_the_tray_is_built_from_the_follow_graph(
    client, created_user, second_user, db_session
):
    """A public stranger's story is VIEWABLE (permalink, profile) but not
    RECOMMENDED — the tray is your follow graph, not the whole product."""
    await _claim(client, created_user, "poster11")
    await _claim(client, second_user, "browser11")
    await _post_story(client, created_user)

    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert tray["entries"] == []

    await client.post("/v1/social/users/poster11/follow", headers=_headers(second_user))
    tray = assert_envelope_ok(
        await client.get("/v1/social/stories/tray", headers=_headers(second_user))
    )
    assert [e["author"]["username"] for e in tray["entries"]] == ["poster11"]
    # The preview's kind ships with the entry so clients never hand a video
    # clip to an image view.
    assert tray["entries"][0]["kind"] == "image"
