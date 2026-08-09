"""Content screening and the review queue.

The properties that matter, in order of how badly a bug would hurt:

1. **A vendor outage must never stop people posting.** Screening fails open
   — but the content still lands in the queue, so "we couldn't check it"
   never silently becomes "nobody ever looked at it".
2. **Zero-tolerance content is refused**, and the refusal itself is
   auditable — that is how we find out the classifier is wrong about a word
   collectors use every day.
3. **Reporting is not a vote.** The same person reporting twice is one case.
4. **Resolving answers duplicates.** Ten reports about one post close
   together, or the queue becomes noise nobody reads.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.user import User
from app.social import moderation
from app.social.models import SocialModerationCase, SocialPost
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _make_admin(db) -> User:
    """A DB-flag admin, created in the test body — the same pattern the
    other admin suites use to dodge a fixture-ordering quirk between
    `client` and the in-memory engine."""
    user = User(email=f"admin+{uuid4().hex[:8]}@example.com", is_admin=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _claim(client, user, username: str) -> dict:
    return assert_envelope_ok(
        await client.put(
            "/v1/social/me", json={"username": username}, headers=_headers(user)
        )
    )


async def _post(client, user, body: str) -> dict:
    return assert_envelope_ok(
        await client.post(
            "/v1/social/posts", data={"body": body}, headers=_headers(user)
        ),
        expected_status=201,
    )


@pytest.fixture
def allow_all(monkeypatch):
    """Screening that passes everything — the default for feed tests."""

    async def _screen(text=None, images=None):
        return moderation.Verdict()

    monkeypatch.setattr(moderation, "screen", _screen)


def _verdict(action: str, categories: list[str]):
    async def _screen(text=None, images=None):
        return moderation.Verdict(
            action=action,
            categories=categories,
            score=0.97,
            detail=f"test: {', '.join(categories)}",
        )

    return _screen


# ── The classifier's own decision logic (no network) ──


def test_zero_tolerance_categories_are_the_ones_we_will_not_publish():
    # A deliberate, short list. If this grows without thought, ordinary
    # collector vocabulary starts getting refused.
    assert "sexual/minors" in moderation.ZERO_TOLERANCE
    assert "violence/graphic" in moderation.ZERO_TOLERANCE
    # "harassment" alone is NOT zero-tolerance — it publishes and queues.
    assert "harassment" not in moderation.ZERO_TOLERANCE


@pytest.mark.asyncio
async def test_screening_nothing_is_allowed_without_calling_a_provider():
    assert (await moderation.screen(None, [])).action == moderation.ALLOW
    assert (await moderation.screen("   ")).action == moderation.ALLOW


@pytest.mark.asyncio
async def test_no_key_means_screening_is_off_not_that_everything_is_suspect(
    monkeypatch,
):
    """An unconfigured environment has screening switched off deliberately.

    Queueing every post there would make the review queue 100% of the
    content — noise a moderator learns to ignore, which is worse than no
    queue. Distinct from a vendor we tried and couldn't reach (below).
    """
    monkeypatch.setattr(moderation, "enabled", lambda: False)
    verdict = await moderation.screen("anything at all")
    assert verdict.action == moderation.ALLOW
    assert not verdict.needs_review


@pytest.mark.asyncio
async def test_a_provider_error_fails_open_but_still_queues(monkeypatch):
    async def _boom(_parts):
        raise RuntimeError("openai is down")

    monkeypatch.setattr(moderation, "enabled", lambda: True)
    monkeypatch.setattr(moderation, "_classify", _boom)

    verdict = await moderation.screen("a normal post about charizard")
    assert not verdict.blocked  # posting keeps working
    assert verdict.needs_review  # …and a human will still see it


# ── Posting ──


@pytest.mark.asyncio
async def test_zero_tolerance_content_is_refused_and_leaves_an_audit_case(
    client, db_session, created_user, monkeypatch
):
    await _claim(client, created_user, "poster1")
    monkeypatch.setattr(
        moderation, "screen", _verdict(moderation.BLOCK, ["sexual/minors"])
    )

    resp = await client.post(
        "/v1/social/posts",
        data={"body": "something awful"},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)

    # Nothing was stored…
    assert (await db_session.execute(select(SocialPost))).scalars().all() == []
    # …but the refusal is on the record, with a copy of what was refused.
    case = (await db_session.execute(select(SocialModerationCase))).scalars().one()
    assert case.source == "auto"
    assert case.status == "removed"
    assert case.excerpt == "something awful"


@pytest.mark.asyncio
async def test_flagged_content_publishes_and_opens_a_case(
    client, db_session, created_user, monkeypatch
):
    """A collector app says "sick", "insane", "killer" and "steal" all day.
    Auto-deleting on the classifier's say-so would delete real posts."""
    await _claim(client, created_user, "poster2")
    monkeypatch.setattr(
        moderation, "screen", _verdict(moderation.REVIEW, ["harassment"])
    )

    post = await _post(client, created_user, "that trade was a steal lol")
    case = (await db_session.execute(select(SocialModerationCase))).scalars().one()
    assert case.status == "open"
    assert case.target_id == UUID(post["id"])

    # And it is genuinely visible in the feed.
    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "mine"}, headers=_headers(created_user)
        )
    )
    assert [i["id"] for i in feed["items"]] == [post["id"]]


@pytest.mark.asyncio
async def test_clean_content_opens_no_case(client, db_session, created_user, allow_all):
    await _claim(client, created_user, "poster3")
    await _post(client, created_user, "pulled a charizard #pokemon")
    assert (
        await db_session.execute(select(SocialModerationCase))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_a_blocked_comment_is_refused(
    client, db_session, created_user, monkeypatch
):
    await _claim(client, created_user, "poster4")

    async def _clean(text=None, images=None):
        return moderation.Verdict()

    monkeypatch.setattr(moderation, "screen", _clean)
    post = await _post(client, created_user, "nice pull")

    monkeypatch.setattr(
        moderation, "screen", _verdict(moderation.BLOCK, ["hate/threatening"])
    )
    resp = await client.post(
        f"/v1/social/posts/{post['id']}/comments",
        json={"body": "vile"},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


# ── Reports ──


@pytest.mark.asyncio
async def test_reporting_twice_is_one_case(client, db_session, created_user, allow_all):
    author = await make_user(db_session)
    await _claim(client, author, "author9")
    await _claim(client, created_user, "reporter9")
    post = await _post(client, author, "look at this")

    body = {"target_type": "post", "target_id": post["id"], "reason": "spam"}
    first = assert_envelope_ok(
        await client.post(
            "/v1/social/reports", json=body, headers=_headers(created_user)
        ),
        expected_status=201,
    )
    second = assert_envelope_ok(
        await client.post(
            "/v1/social/reports", json=body, headers=_headers(created_user)
        ),
        expected_status=201,
    )
    assert first["id"] == second["id"]

    rows = (await db_session.execute(select(SocialModerationCase))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_you_cannot_report_your_own_post(client, created_user, allow_all):
    await _claim(client, created_user, "selfreport")
    post = await _post(client, created_user, "mine")
    resp = await client.post(
        "/v1/social/reports",
        json={"target_type": "post", "target_id": post["id"], "reason": "spam"},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_an_unknown_reason_is_refused(
    client, db_session, created_user, allow_all
):
    author = await make_user(db_session)
    await _claim(client, author, "author10")
    await _claim(client, created_user, "reporter10")
    post = await _post(client, author, "hi")
    resp = await client.post(
        "/v1/social/reports",
        json={"target_type": "post", "target_id": post["id"], "reason": "vibes"},
        headers=_headers(created_user),
    )
    assert_envelope_error(resp, expected_status=422)


# ── The admin queue ──


@pytest.mark.asyncio
async def test_queue_is_admin_only(client, db_session, created_user, allow_all):
    resp = await client.get(
        "/v1/admin/social/moderation", headers=_headers(created_user)
    )
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_resolving_one_case_answers_every_duplicate(
    client, db_session, created_user, allow_all
):
    admin_user = await _make_admin(db_session)
    author = await make_user(db_session)
    other = await make_user(db_session)
    await _claim(client, author, "author11")
    await _claim(client, created_user, "reporter11")
    await _claim(client, other, "reporter12")
    post = await _post(client, author, "contested")

    body = {"target_type": "post", "target_id": post["id"], "reason": "spam"}
    for reporter in (created_user, other):
        assert_envelope_ok(
            await client.post(
                "/v1/social/reports", json=body, headers=_headers(reporter)
            ),
            expected_status=201,
        )

    queue = assert_envelope_ok(
        await client.get("/v1/admin/social/moderation", headers=_headers(admin_user))
    )
    assert queue["open_count"] == 2

    resolved = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{queue['items'][0]['id']}/resolve",
            params={"action": "remove"},
            headers=_headers(admin_user),
        )
    )
    assert resolved["status"] == "removed"

    # Both cases closed, and the post is gone from the feed.
    after = assert_envelope_ok(
        await client.get("/v1/admin/social/moderation", headers=_headers(admin_user))
    )
    assert after["open_count"] == 0

    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "mine"}, headers=_headers(author)
        )
    )
    assert feed["items"] == []


@pytest.mark.asyncio
async def test_dismissing_leaves_the_post_up(
    client, db_session, created_user, allow_all
):
    admin_user = await _make_admin(db_session)
    author = await make_user(db_session)
    await _claim(client, author, "author12")
    await _claim(client, created_user, "reporter13")
    post = await _post(client, author, "fine actually")

    assert_envelope_ok(
        await client.post(
            "/v1/social/reports",
            json={"target_type": "post", "target_id": post["id"], "reason": "other"},
            headers=_headers(created_user),
        ),
        expected_status=201,
    )
    queue = assert_envelope_ok(
        await client.get("/v1/admin/social/moderation", headers=_headers(admin_user))
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{queue['items'][0]['id']}/resolve",
            params={"action": "dismiss"},
            headers=_headers(admin_user),
        )
    )

    feed = assert_envelope_ok(
        await client.get(
            "/v1/social/feed", params={"tab": "mine"}, headers=_headers(author)
        )
    )
    assert [i["id"] for i in feed["items"]] == [post["id"]]


# ── Profile pictures ──
#
# An avatar is the widest-reach image in the product: a caption is seen by
# whoever opens the post, but a picture rides along on every feed row, every
# comment and every follower list the account appears in.


def _png() -> bytes:
    """A real 1x1 PNG — the upload path validates content, not just headers."""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


async def _upload_avatar(client, user):
    return await client.post(
        "/v1/social/me/avatar",
        files={"image": ("me.png", _png(), "image/png")},
        headers=_headers(user),
    )


@pytest.mark.asyncio
async def test_a_clean_avatar_uploads_and_opens_no_case(
    client, db_session, created_user, allow_all
):
    await _claim(client, created_user, "picgood")
    profile = assert_envelope_ok(await _upload_avatar(client, created_user))
    assert profile["avatar_url"]
    assert (
        await db_session.execute(select(SocialModerationCase))
    ).scalars().all() == []


@pytest.mark.asyncio
async def test_a_zero_tolerance_avatar_is_refused_and_never_stored(
    client, db_session, created_user, monkeypatch
):
    await _claim(client, created_user, "picbad")
    monkeypatch.setattr(moderation, "screen", _verdict(moderation.BLOCK, ["sexual"]))

    resp = await _upload_avatar(client, created_user)
    assert_envelope_error(resp, expected_status=422)

    # The profile still has no picture…
    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["profile"]["avatar_url"] is None
    # …and the attempt is on the record.
    case = (await db_session.execute(select(SocialModerationCase))).scalars().one()
    assert case.target_type == "profile"
    assert case.status == "removed"


@pytest.mark.asyncio
async def test_a_doubtful_avatar_is_stored_but_queued(
    client, db_session, created_user, monkeypatch
):
    await _claim(client, created_user, "picmeh")
    monkeypatch.setattr(moderation, "screen", _verdict(moderation.REVIEW, ["violence"]))

    profile = assert_envelope_ok(await _upload_avatar(client, created_user))
    assert profile["avatar_url"]  # not punished for a maybe

    case = (await db_session.execute(select(SocialModerationCase))).scalars().one()
    assert case.status == "open"
    assert case.target_type == "profile"


@pytest.mark.asyncio
async def test_removing_a_profile_case_clears_the_picture(
    client, db_session, created_user, monkeypatch
):
    admin_user = await _make_admin(db_session)
    await _claim(client, created_user, "picpull")
    monkeypatch.setattr(moderation, "screen", _verdict(moderation.REVIEW, ["violence"]))
    assert_envelope_ok(await _upload_avatar(client, created_user))

    queue = assert_envelope_ok(
        await client.get("/v1/admin/social/moderation", headers=_headers(admin_user))
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{queue['items'][0]['id']}/resolve",
            params={"action": "remove"},
            headers=_headers(admin_user),
        )
    )

    me = assert_envelope_ok(
        await client.get("/v1/social/me", headers=_headers(created_user))
    )
    assert me["profile"]["avatar_url"] is None
