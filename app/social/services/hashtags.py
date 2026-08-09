"""Hashtags: the trending chip row, a tag's feed, and tag search."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social.models import SocialPost, SocialPostHashtag
from app.social.schemas import FeedRead, HashtagRead
from app.social.services.feed_common import (
    DEFAULT_PAGE,
    base_post_query,
    decode_cursor,
    encode_cursor,
    post_payloads,
    visible_post_ids,
)

#: Trending looks at the last week. A day is too jumpy to be a stable row of
#: chips; a month stops being "trending" and becomes "our most-used tags".
TRENDING_WINDOW = timedelta(days=7)


def normalise(tag: str) -> str:
    """Accept ``#Pokemon``, ``Pokemon`` or ``pokemon`` and mean one thing."""
    return tag.strip().lstrip("#").lower()


async def trending(
    db: AsyncSession, viewer: User, *, limit: int = 12
) -> list[HashtagRead]:
    """The chip row above the For You feed.

    Counts only tags on posts this viewer may actually open — a chip that
    leads to an empty page because the posts behind it are private is worse
    than no chip.
    """
    since = datetime.now(UTC) - TRENDING_WINDOW
    rows = (
        await db.execute(
            select(SocialPostHashtag.tag, func.count().label("n"))
            .select_from(SocialPostHashtag)
            .join(SocialPost, SocialPost.id == SocialPostHashtag.post_id)
            .where(
                SocialPost.id.in_(visible_post_ids(viewer.id)),
                SocialPost.created_at >= since,
            )
            .group_by(SocialPostHashtag.tag)
            .order_by(func.count().desc(), SocialPostHashtag.tag.asc())
            .limit(limit)
        )
    ).all()
    return [HashtagRead(tag=tag, post_count=int(n)) for tag, n in rows]


async def search(
    db: AsyncSession, viewer: User, query: str, *, limit: int = 10
) -> list[HashtagRead]:
    """Tag typeahead — prefix matches first, then anything containing it."""
    needle = normalise(query)
    if not needle:
        return []
    rows = (
        await db.execute(
            select(SocialPostHashtag.tag, func.count().label("n"))
            .join(SocialPost, SocialPost.id == SocialPostHashtag.post_id)
            .where(
                SocialPost.id.in_(visible_post_ids(viewer.id)),
                SocialPostHashtag.tag.like(f"%{needle}%"),
            )
            .group_by(SocialPostHashtag.tag)
            # Prefix matches rank above mid-word ones: someone typing "poke"
            # means #pokemon, not #funkopokedex.
            .order_by(
                SocialPostHashtag.tag.like(f"{needle}%").desc(),
                func.count().desc(),
                SocialPostHashtag.tag.asc(),
            )
            .limit(limit)
        )
    ).all()
    return [HashtagRead(tag=tag, post_count=int(n)) for tag, n in rows]


async def tag_feed(
    db: AsyncSession,
    viewer: User,
    tag: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> FeedRead:
    """Every post carrying this tag that the viewer may see, newest first."""
    needle = normalise(tag)
    stmt = base_post_query(viewer.id).where(
        SocialPost.id.in_(
            select(SocialPostHashtag.post_id).where(SocialPostHashtag.tag == needle)
        )
    )
    seek = decode_cursor(cursor)
    if seek is not None:
        created_at, last_id = seek
        stmt = stmt.where(
            (SocialPost.created_at < created_at)
            | ((SocialPost.created_at == created_at) & (SocialPost.id < last_id))
        )
    rows = (
        (
            await db.execute(
                stmt.order_by(SocialPost.created_at.desc(), SocialPost.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .unique()
        .all()
    )
    has_more = len(rows) > limit
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]
    items = await post_payloads(db, viewer, page)
    return FeedRead(
        items=items,
        next_cursor=(
            encode_cursor(page[-1][0].created_at, page[-1][0].id)
            if has_more and page
            else None
        ),
    )


__all__ = ["normalise", "search", "tag_feed", "trending"]
