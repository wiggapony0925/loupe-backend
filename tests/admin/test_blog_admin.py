"""Router tests for `/v1/admin/blog` — editorial CRUD behind the admin gate.

The blog is the one surface where an admin action is instantly world-readable,
so the rules that matter here are about *publication*: a draft must never leak
to the public routes, publishing must stamp the moment it happened, and an
edit to an already-live post must not rewrite that moment (the "published on"
date readers and feeds see).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


def _post(**overrides) -> dict:
    payload = {
        "title": "Grading season is here",
        "excerpt": "What changed in the submission queue.",
        "body": "The full article.",
        "tag": "Update",
        "author": "The Loupe Team",
        "read_minutes": 4,
        "status": "draft",
    }
    payload.update(overrides)
    return payload


async def _create(client, headers, **overrides) -> dict:
    return assert_envelope_ok(
        await client.post("/v1/admin/blog", headers=headers, json=_post(**overrides)),
        expected_status=201,
    )


# ── RULE: the whole subtree is admin-only, reads included ────────────────


@pytest.mark.asyncio
async def test_blog_admin_rejects_anonymous_callers(client):
    resp = await client.get("/v1/admin/blog")
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
async def test_blog_admin_rejects_an_ordinary_signed_in_user(client, auth_headers):
    """Unpublished drafts are visible here, so even the *read* routes are
    staff-only — an ordinary account must not be able to preview them."""
    resp = await client.get("/v1/admin/blog", headers=auth_headers)
    assert_envelope_error(resp, expected_status=403)


@pytest.mark.asyncio
async def test_creating_a_post_rejects_an_ordinary_signed_in_user(client, auth_headers):
    resp = await client.post("/v1/admin/blog", headers=auth_headers, json=_post())
    assert_envelope_error(resp, expected_status=403)


@pytest.mark.asyncio
async def test_deleting_a_post_rejects_an_ordinary_signed_in_user(
    client, admin_headers, auth_headers
):
    row = await _create(client, admin_headers)
    resp = await client.delete(f"/v1/admin/blog/{row['id']}", headers=auth_headers)
    assert_envelope_error(resp, expected_status=403)


# ── Create ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creating_a_draft_derives_a_slug_and_stays_unpublished(
    client, admin_headers
):
    row = await _create(client, admin_headers)
    assert row["slug"] == "grading-season-is-here"
    assert row["status"] == "draft"
    assert row["published_at"] is None

    public = assert_envelope_ok(await client.get("/v1/blog/posts"))
    assert public == [], "a draft must never reach the public list"
    assert_envelope_error(
        await client.get(f"/v1/blog/posts/{row['slug']}"), expected_status=404
    )


@pytest.mark.asyncio
async def test_publishing_at_creation_stamps_published_at_and_goes_live(
    client, admin_headers
):
    row = await _create(client, admin_headers, status="published")
    assert row["status"] == "published"
    assert row["published_at"] is not None

    served = assert_envelope_ok(await client.get(f"/v1/blog/posts/{row['slug']}"))
    assert served["id"] == row["id"]


@pytest.mark.asyncio
async def test_a_second_post_with_the_same_title_gets_a_suffixed_slug(
    client, admin_headers
):
    """Slugs are the public URL and are unique in the DB; two posts sharing a
    title must not collide (or blow up with an integrity error)."""
    first = await _create(client, admin_headers)
    second = await _create(client, admin_headers)
    assert first["slug"] == "grading-season-is-here"
    assert second["slug"] == "grading-season-is-here-2"


@pytest.mark.asyncio
async def test_creating_a_post_rejects_a_one_character_title(client, admin_headers):
    resp = await client.post(
        "/v1/admin/blog", headers=admin_headers, json=_post(title="A")
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_creating_a_post_rejects_a_zero_minute_read_time(client, admin_headers):
    resp = await client.post(
        "/v1/admin/blog", headers=admin_headers, json=_post(read_minutes=0)
    )
    assert_envelope_error(resp, expected_status=422)


# ── List / get ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_admin_list_shows_drafts_alongside_published_posts(
    client, admin_headers
):
    """The editorial view is the one place both states are visible — that is
    the difference between it and `/v1/blog/posts`."""
    await _create(client, admin_headers, title="A draft in progress")
    await _create(client, admin_headers, title="Already live", status="published")

    rows = assert_envelope_ok(await client.get("/v1/admin/blog", headers=admin_headers))
    assert {r["status"] for r in rows} == {"draft", "published"}

    public = assert_envelope_ok(await client.get("/v1/blog/posts"))
    assert [p["title"] for p in public] == ["Already live"]


@pytest.mark.asyncio
async def test_getting_a_post_by_id_returns_the_draft_body(client, admin_headers):
    row = await _create(client, admin_headers)
    fetched = assert_envelope_ok(
        await client.get(f"/v1/admin/blog/{row['id']}", headers=admin_headers)
    )
    assert fetched["id"] == row["id"]
    assert fetched["body"] == "The full article."


@pytest.mark.asyncio
async def test_getting_an_unknown_post_404s(client, admin_headers):
    resp = await client.get(f"/v1/admin/blog/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_getting_a_post_with_a_malformed_id_is_a_422(client, admin_headers):
    resp = await client.get("/v1/admin/blog/not-a-uuid", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── Update ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_partial_update_leaves_untouched_fields_alone(client, admin_headers):
    """`BlogPostUpdate` is send-only-what-changed; a PATCH that omits `body`
    must not blank it."""
    row = await _create(client, admin_headers)
    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"excerpt": "A sharper excerpt."},
        )
    )
    assert updated["excerpt"] == "A sharper excerpt."
    assert updated["body"] == "The full article."
    assert updated["title"] == row["title"]


@pytest.mark.asyncio
async def test_publishing_a_draft_stamps_published_at(client, admin_headers):
    row = await _create(client, admin_headers)
    assert row["published_at"] is None

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "published"},
        )
    )
    assert updated["published_at"] is not None
    assert_envelope_ok(await client.get(f"/v1/blog/posts/{updated['slug']}"))


@pytest.mark.asyncio
async def test_editing_a_live_post_does_not_move_its_publication_date(
    client, admin_headers
):
    """`published_at` is the date readers and feeds see. Fixing a typo three
    weeks later must not republish the article to the top of the list."""
    row = await _create(client, admin_headers, status="published")
    first_published = row["published_at"]

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"body": "Fixed a typo.", "status": "published"},
        )
    )
    assert updated["published_at"] == first_published


@pytest.mark.asyncio
async def test_unpublishing_pulls_the_post_from_the_public_routes(
    client, admin_headers
):
    row = await _create(client, admin_headers, status="published")
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "draft"},
        )
    )
    assert_envelope_error(
        await client.get(f"/v1/blog/posts/{row['slug']}"), expected_status=404
    )
    assert assert_envelope_ok(await client.get("/v1/blog/posts")) == []


@pytest.mark.asyncio
async def test_renaming_the_slug_keeps_it_unique_against_other_posts(
    client, admin_headers
):
    await _create(client, admin_headers, title="Taken slug")
    row = await _create(client, admin_headers, title="Second post")

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"slug": "taken-slug"},
        )
    )
    assert updated["slug"] == "taken-slug-2"


@pytest.mark.asyncio
async def test_updating_an_unknown_post_404s(client, admin_headers):
    resp = await client.patch(
        f"/v1/admin/blog/{uuid.uuid4()}",
        headers=admin_headers,
        json={"title": "Ghost post"},
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_updating_a_post_is_audit_logged_with_only_the_changed_fields(
    client, admin_headers, db_session
):
    """The audit payload is the diff an operator would be asked to explain, so
    it records what was sent — not the whole row."""
    from sqlalchemy import select

    from app.models.audit import AuditLog

    row = await _create(client, admin_headers)
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"tag": "Product"},
        )
    )
    logged = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "blog.update")
            )
        )
        .scalars()
        .all()
    )
    assert len(logged) == 1
    assert logged[0].payload == {"tag": "Product"}
    assert logged[0].target_id == row["id"]


# ── Announcements ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publishing_announces_the_post_once_in_the_inbox(
    client, admin_headers, db_session, admin_user
):
    """Publishing puts the article in every user's inbox, keyed `blog:<id>` so
    a post that is edited, unpublished and republished still only announces
    itself once."""
    from sqlalchemy import select

    from app.models.notification import Notification

    row = await _create(client, admin_headers, status="published")
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "draft"},
        )
    )
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "published"},
        )
    )

    notes = (
        (
            await db_session.execute(
                select(Notification).where(Notification.user_id == admin_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert [n.kind for n in notes] == ["blog_post"], "republishing must not re-notify"


@pytest.mark.asyncio
async def test_republishing_does_not_re_email_the_announcement_list(
    client, admin_headers, monkeypatch
):
    """A post announces itself exactly once, by mail as well as in the inbox.
    Pulling an article back to draft and publishing it again is a correction,
    not news, and must not blast the whole announcement list a second time —
    the email follows the same once-per-post rule as the inbox item."""
    from app.services import email_service

    sent: list[str] = []

    async def _fake_send(recipients, **kwargs) -> None:
        sent.append(kwargs["title"])

    monkeypatch.setattr(email_service, "send_blog_announcement", _fake_send)

    row = await _create(client, admin_headers, status="published")
    assert len(sent) == 1

    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "draft"},
        )
    )
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "published"},
        )
    )
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_publishing_a_draft_emails_the_announcement_list_once(
    client, admin_headers, monkeypatch
):
    """The other half of the once-per-post rule: a post written as a draft and
    published later still gets its one announcement email, at the moment it
    goes live."""
    from app.services import email_service

    sent: list[str] = []

    async def _fake_send(recipients, **kwargs) -> None:
        sent.append(kwargs["title"])

    monkeypatch.setattr(email_service, "send_blog_announcement", _fake_send)

    row = await _create(client, admin_headers)
    assert sent == []

    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"status": "published"},
        )
    )
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_editing_a_live_post_does_not_re_email_subscribers(
    client, admin_headers, monkeypatch
):
    """A typo fix is not news. Only the draft→published transition mails."""
    from app.services import email_service

    sent: list[str] = []

    async def _fake_send(recipients, **kwargs) -> None:
        sent.append(kwargs["title"])

    monkeypatch.setattr(email_service, "send_blog_announcement", _fake_send)

    row = await _create(client, admin_headers, status="published")
    assert_envelope_ok(
        await client.patch(
            f"/v1/admin/blog/{row['id']}",
            headers=admin_headers,
            json={"body": "Fixed a typo.", "status": "published"},
        )
    )
    assert len(sent) == 1


# ── Delete ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_post_removes_it_everywhere(client, admin_headers):
    row = await _create(client, admin_headers, status="published")
    resp = await client.delete(f"/v1/admin/blog/{row['id']}", headers=admin_headers)
    assert resp.status_code == 204

    assert_envelope_error(
        await client.get(f"/v1/admin/blog/{row['id']}", headers=admin_headers),
        expected_status=404,
    )
    assert_envelope_error(
        await client.get(f"/v1/blog/posts/{row['slug']}"), expected_status=404
    )


@pytest.mark.asyncio
async def test_deleting_an_unknown_post_404s(client, admin_headers):
    """Deleting twice must not report success — the second call is a mistake
    worth surfacing (usually a stale portal tab)."""
    resp = await client.delete(f"/v1/admin/blog/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)
