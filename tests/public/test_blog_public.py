"""`GET /v1/blog/posts` and `/v1/blog/posts/{slug}` — the public blog.

Public reads must never expose a draft: an unpublished post is an editor's
work-in-progress, and the marketing site links to these URLs directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.blog import BlogPost
from app.models.enums import BlogStatusEnum
from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _post(
    db_session,
    *,
    slug: str,
    title: str | None = None,
    status: BlogStatusEnum = BlogStatusEnum.published,
    published_at: datetime | None = None,
) -> BlogPost:
    row = BlogPost(
        slug=slug,
        title=title or slug.replace("-", " ").title(),
        excerpt="An excerpt.",
        body="The body.",
        status=status.value,
        published_at=published_at,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


# ------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_list_returns_published_posts_without_auth(client, db_session):
    """The blog is the public face of the product — no header required."""
    await _post(db_session, slug="hello-world")
    rows = assert_envelope_ok(await client.get("/v1/blog/posts"))
    assert [r["slug"] for r in rows] == ["hello-world"]
    assert rows[0]["status"] == "published"
    assert rows[0]["body"] == "The body."


@pytest.mark.asyncio
async def test_list_hides_drafts(client, db_session):
    """A draft is unfinished writing. Listing it publicly would leak
    unannounced work the moment an editor saves."""
    await _post(db_session, slug="live-post")
    await _post(db_session, slug="secret-post", status=BlogStatusEnum.draft)

    rows = assert_envelope_ok(await client.get("/v1/blog/posts"))
    assert [r["slug"] for r in rows] == ["live-post"]


@pytest.mark.asyncio
async def test_list_is_newest_published_first(client, db_session):
    """Readers expect the top of the page to be the latest article, so the
    ordering key is the publish date, not the row's creation order."""
    now = datetime.now(UTC)
    await _post(db_session, slug="older", published_at=now - timedelta(days=5))
    await _post(db_session, slug="newest", published_at=now)
    await _post(db_session, slug="middle", published_at=now - timedelta(days=1))

    rows = assert_envelope_ok(await client.get("/v1/blog/posts"))
    assert [r["slug"] for r in rows] == ["newest", "middle", "older"]


@pytest.mark.asyncio
async def test_limit_and_offset_page_through_the_archive(client, db_session):
    now = datetime.now(UTC)
    for i in range(3):
        await _post(db_session, slug=f"post-{i}", published_at=now - timedelta(days=i))

    first = assert_envelope_ok(await client.get("/v1/blog/posts?limit=2"))
    assert [r["slug"] for r in first] == ["post-0", "post-1"]

    second = assert_envelope_ok(await client.get("/v1/blog/posts?limit=2&offset=2"))
    assert [r["slug"] for r in second] == ["post-2"]

    past_end = assert_envelope_ok(await client.get("/v1/blog/posts?offset=99"))
    assert past_end == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
async def test_pagination_bounds_are_enforced(client, query):
    """`limit` caps at 100 so a crawler can't ask for the whole archive in
    one request; a negative offset is meaningless."""
    assert_envelope_error(
        await client.get(f"/v1/blog/posts?{query}"), expected_status=422
    )


# ------------------------------------------------------------------ detail


@pytest.mark.asyncio
async def test_detail_returns_the_full_post_by_slug(client, db_session):
    """Posts are addressed by slug, not id, because the slug is the URL the
    marketing site and search engines already hold."""
    await _post(db_session, slug="how-grading-works", title="How Grading Works")
    body = assert_envelope_ok(await client.get("/v1/blog/posts/how-grading-works"))
    assert body["slug"] == "how-grading-works"
    assert body["title"] == "How Grading Works"
    assert body["body"] == "The body."


@pytest.mark.asyncio
async def test_detail_404s_for_an_unknown_slug(client):
    assert_envelope_error(
        await client.get("/v1/blog/posts/no-such-post"), expected_status=404
    )


@pytest.mark.asyncio
async def test_a_draft_slug_is_indistinguishable_from_a_missing_post(
    client, db_session
):
    """Guessing a slug must not confirm that an unannounced post exists, so
    a draft answers with the same 404 as a slug that was never used."""
    await _post(db_session, slug="unreleased-feature", status=BlogStatusEnum.draft)
    assert_envelope_error(
        await client.get("/v1/blog/posts/unreleased-feature"), expected_status=404
    )
