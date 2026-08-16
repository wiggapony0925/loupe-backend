"""Admin control of the Community surface (`/v1/admin/social`).

Two levers a moderator pulls, tested from the portal's side:

* **Resolving a case** — dismiss it, or remove what it points at. Removal is
  destructive to someone else's content, so the gate and the 404/422 shapes
  matter as much as the happy path.
* **Force-expiring a story** — the dev tool that makes the 24-hour lifecycle
  testable without waiting a day. Expiring must take the story off the live
  surfaces while leaving the row (and therefore the author's archive) alone;
  "expire" and "delete" being one button would be a data-loss bug.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.jwt import issue_token
from app.models.user import User
from app.social import moderation
from app.social.models import (
    SocialModerationCase,
    SocialPost,
    SocialProfile,
    SocialStory,
    SocialStoryComment,
    SocialStoryView,
)
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


def _headers(user: User) -> dict[str, str]:
    token, _ = issue_token(user.id, "access", {"ver": user.token_version})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_user(db_session):
    """A staff moderator — a DB-backed admin grant, not a super-admin.

    Moderation is deliberately reachable by an ordinary admin: nobody should
    need owner credentials to take down a scam listing.
    """
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    return _headers(admin_user)


@pytest.fixture
def allow_all(monkeypatch):
    """Screening that passes everything — these tests are about the queue, not
    about the classifier's opinion of the text."""

    async def _screen(text=None, images=None, **_policy):
        return moderation.Verdict()

    monkeypatch.setattr(moderation, "screen", _screen)


async def _claim(client, user: User, username: str) -> dict:
    return assert_envelope_ok(
        await client.put(
            "/v1/social/me", json={"username": username}, headers=_headers(user)
        )
    )


async def _reported_post(client, author: User, reporter: User) -> tuple[dict, dict]:
    """A published post plus the open case a user opened against it."""
    post = assert_envelope_ok(
        await client.post(
            "/v1/social/posts", data={"body": "buy my cards"}, headers=_headers(author)
        ),
        expected_status=201,
    )
    case = assert_envelope_ok(
        await client.post(
            "/v1/social/reports",
            json={"target_type": "post", "target_id": post["id"], "reason": "spam"},
            headers=_headers(reporter),
        ),
        expected_status=201,
    )
    return post, case


async def _story(db_session, author: User, *, live: bool = True) -> SocialStory:
    """A story row written straight to the DB.

    Built by hand rather than through the seeder so the test controls the
    clock: the seeder derives `expires_at` from real media it copies, and
    these tests are about what expiry DOES, not how a story is born.
    """
    now = datetime.now(UTC)
    story = SocialStory(
        author_id=author.id,
        storage_key=f"social/stories/{uuid.uuid4()}",
        content_type="image/jpeg",
        width=1080,
        height=1920,
        caption="mail day",
        created_at=now - timedelta(hours=1),
        expires_at=(now + timedelta(hours=23)) if live else (now - timedelta(hours=1)),
    )
    db_session.add(story)
    await db_session.commit()
    await db_session.refresh(story)
    return story


# ── RULE: both levers are admin-only ──

ROUTES = [
    ("/v1/admin/social/moderation/{oid}/resolve", {"action": "dismiss"}),
    ("/v1/admin/social/stories/{oid}/expire", None),
]
ROUTE_IDS = [template for template, _ in ROUTES]


@pytest.mark.asyncio
@pytest.mark.parametrize(("template", "params"), ROUTES, ids=ROUTE_IDS)
async def test_admin_social_routes_challenge_an_anonymous_caller(
    client, template, params
):
    resp = await client.post(template.format(oid=uuid.uuid4()), params=params)
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(("template", "params"), ROUTES, ids=ROUTE_IDS)
async def test_admin_social_routes_refuse_an_ordinary_signed_in_user(
    client, auth_headers, template, params
):
    """A community member must not be able to delete another member's content
    or end their story, however well they know the URL."""
    resp = await client.post(
        template.format(oid=uuid.uuid4()), params=params, headers=auth_headers
    )
    assert_envelope_error(resp, expected_status=403)


# ── Resolving a moderation case ──


@pytest.mark.asyncio
async def test_removing_a_case_takes_the_reported_post_down(
    client, db_session, admin_headers, created_user, second_user, allow_all
):
    """ "Remove" is the whole point of the queue — a case that closes without
    the content coming down is a moderator's time wasted."""
    await _claim(client, created_user, "spammer1")
    await _claim(client, second_user, "reporter1")
    post, case = await _reported_post(client, created_user, second_user)
    post_id = uuid.UUID(post["id"])

    resolved = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{case['id']}/resolve",
            params={"action": "remove"},
            headers=admin_headers,
        )
    )
    assert resolved["status"] == "removed"
    assert resolved["resolved_at"] is not None

    db_session.expire_all()
    row = await db_session.get(SocialPost, post_id)
    assert row is not None  # soft delete: the evidence survives the takedown
    assert row.deleted_at is not None


@pytest.mark.asyncio
async def test_dismissing_a_case_leaves_the_post_published(
    client, db_session, admin_headers, created_user, second_user, allow_all
):
    """Most reports are wrong. Dismissal has to be a real outcome, not a
    softer removal."""
    await _claim(client, created_user, "author2")
    await _claim(client, second_user, "reporter2")
    post, case = await _reported_post(client, created_user, second_user)
    post_id = uuid.UUID(post["id"])

    resolved = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{case['id']}/resolve",
            params={"action": "dismiss"},
            headers=admin_headers,
        )
    )
    assert resolved["status"] == "dismissed"

    db_session.expire_all()
    row = await db_session.get(SocialPost, post_id)
    assert row is not None
    assert row.deleted_at is None


@pytest.mark.asyncio
async def test_resolving_names_the_moderator_who_did_it(
    client, db_session, admin_user, admin_headers, created_user, second_user, allow_all
):
    """A takedown is an accountable act. `resolved_by_id` is also what
    separates a human removal from the classifier's refusal in the queue
    tabs, so leaving it null would put staff decisions in the wrong bucket."""
    await _claim(client, created_user, "author3")
    await _claim(client, second_user, "reporter3")
    _, case = await _reported_post(client, created_user, second_user)
    case_id, actor_id = uuid.UUID(case["id"]), admin_user.id

    assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/moderation/{case_id}/resolve",
            params={"action": "remove"},
            headers=admin_headers,
        )
    )

    db_session.expire_all()
    row = await db_session.get(SocialModerationCase, case_id)
    assert row is not None
    assert row.resolved_by_id == actor_id


@pytest.mark.asyncio
async def test_resolving_404s_for_a_case_that_does_not_exist(client, admin_headers):
    resp = await client.post(
        f"/v1/admin/social/moderation/{uuid.uuid4()}/resolve",
        params={"action": "dismiss"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_resolving_rejects_an_action_that_is_not_dismiss_or_remove(
    client, db_session, admin_headers, created_user, second_user, allow_all
):
    """The action is a free-form query string, so the closed set is enforced
    in the service. A typo must not fall through to a silent no-op that
    leaves the case looking handled."""
    await _claim(client, created_user, "author4")
    await _claim(client, second_user, "reporter4")
    _, case = await _reported_post(client, created_user, second_user)

    resp = await client.post(
        f"/v1/admin/social/moderation/{case['id']}/resolve",
        params={"action": "delete"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)

    db_session.expire_all()
    row = await db_session.get(SocialModerationCase, uuid.UUID(case["id"]))
    assert row is not None
    assert row.status == "open"  # still waiting for someone to decide


@pytest.mark.asyncio
async def test_resolving_requires_an_action_at_all(
    client, admin_headers, created_user, second_user, allow_all
):
    """`action` has no default: "resolve" with no verb is ambiguous between
    "let it stand" and "take it down"."""
    await _claim(client, created_user, "author5")
    await _claim(client, second_user, "reporter5")
    _, case = await _reported_post(client, created_user, second_user)

    resp = await client.post(
        f"/v1/admin/social/moderation/{case['id']}/resolve", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


# ── Force-expiring a story ──


@pytest.mark.asyncio
async def test_expiring_a_story_pushes_its_expiry_into_the_past(
    client, db_session, admin_headers, created_user
):
    """A whole second into the past, not "now": every live-surface query is
    `expires_at > now()`, and SQLite's CURRENT_TIMESTAMP has no fractional
    part — set to the same second, the story would stay live until the clock
    ticked over and the button would look broken."""
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller1"))
    story = await _story(db_session, created_user)

    before = datetime.now(UTC)
    expired = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/stories/{story.id}/expire", headers=admin_headers
        )
    )
    assert expired["live"] is False
    assert expired["username"] == "storyteller1"
    # SQLite hands the column back naive; the value was WRITTEN as UTC, so a
    # missing offset means "UTC with the label lost" (the router says the
    # same thing in its own `_aware` helper).
    returned = datetime.fromisoformat(expired["expires_at"])
    if returned.tzinfo is None:
        returned = returned.replace(tzinfo=UTC)
    assert returned < before


@pytest.mark.asyncio
async def test_expiring_a_story_keeps_the_row_for_the_authors_archive(
    client, db_session, admin_headers, created_user
):
    """Expiry and deletion are different acts. The archive is the reason the
    row outlives the 24 hours, so `deleted_at` must stay null."""
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller2"))
    story = await _story(db_session, created_user)
    story_id = story.id

    assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/stories/{story_id}/expire", headers=admin_headers
        )
    )

    db_session.expire_all()
    row = await db_session.get(SocialStory, story_id)
    assert row is not None
    assert row.deleted_at is None


@pytest.mark.asyncio
async def test_expiring_an_already_expired_story_is_harmless(
    client, db_session, admin_headers, created_user
):
    """The portal's list is a snapshot; a second tap on a row that has since
    expired on its own must not error."""
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller3"))
    story = await _story(db_session, created_user, live=False)

    expired = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/stories/{story.id}/expire", headers=admin_headers
        )
    )
    assert expired["live"] is False


@pytest.mark.asyncio
async def test_expiring_404s_for_a_story_that_does_not_exist(client, admin_headers):
    resp = await client.post(
        f"/v1/admin/social/stories/{uuid.uuid4()}/expire", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_expiring_404s_for_a_story_its_author_already_deleted(
    client, db_session, admin_headers, created_user
):
    """A deleted story is gone, not merely invisible — the portal must not be
    able to reach back into it."""
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller4"))
    story = await _story(db_session, created_user)
    story.deleted_at = datetime.now(UTC)
    await db_session.commit()

    resp = await client.post(
        f"/v1/admin/social/stories/{story.id}/expire", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_expire_reports_the_real_engagement_counts(
    client, db_session, admin_headers, created_user, second_user
):
    """Expiring a story ends its run; it does not erase what it earned. The
    expire response must report the same view/comment counts the list computes
    for that row, so the portal doesn't appear to drop the engagement the
    moment the button is pressed.
    """
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller5"))
    story = await _story(db_session, created_user)
    db_session.add(SocialStoryView(story_id=story.id, viewer_id=second_user.id))
    await db_session.commit()

    expired = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/stories/{story.id}/expire", headers=admin_headers
        )
    )
    assert expired["view_count"] == 1
    assert expired["comment_count"] == 0  # no comments were left on it

    listed = assert_envelope_ok(
        await client.get("/v1/admin/social/stories", headers=admin_headers)
    )
    assert [row["view_count"] for row in listed if row["id"] == str(story.id)] == [1]


@pytest.mark.asyncio
async def test_expire_counts_only_the_comments_that_still_stand(
    client, db_session, admin_headers, created_user, second_user
):
    """A removed comment is gone from the count on every surface — the expire
    response filters `deleted_at` exactly as the list query does, so the two
    numbers can never disagree about the same story."""
    db_session.add(SocialProfile(user_id=created_user.id, username="storyteller6"))
    story = await _story(db_session, created_user)
    db_session.add(
        SocialStoryComment(story_id=story.id, author_id=second_user.id, body="mail day")
    )
    db_session.add(
        SocialStoryComment(
            story_id=story.id,
            author_id=second_user.id,
            body="removed by a moderator",
            deleted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    expired = assert_envelope_ok(
        await client.post(
            f"/v1/admin/social/stories/{story.id}/expire", headers=admin_headers
        )
    )
    assert expired["comment_count"] == 1

    listed = assert_envelope_ok(
        await client.get("/v1/admin/social/stories", headers=admin_headers)
    )
    assert [row["comment_count"] for row in listed if row["id"] == str(story.id)] == [1]
