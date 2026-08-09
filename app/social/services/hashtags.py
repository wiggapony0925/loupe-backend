"""Hashtags: the trending chip row, a tag's feed, and tag search."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social.models import (
    SocialPost,
    SocialPostComment,
    SocialPostHashtag,
    SocialPostLike,
)
from app.social.schemas import FeedRead, HashtagRead
from app.social.services.feed_common import (
    DEFAULT_PAGE,
    base_post_query,
    decode_cursor,
    decode_offset,
    encode_cursor,
    encode_offset,
    post_payloads,
    visible_post_ids,
)

#: Trending looks at the last week. A day is too jumpy to be a stable row of
#: chips; a month stops being "trending" and becomes "our most-used tags".
TRENDING_WINDOW = timedelta(days=7)

#: Same weights as the For You ranker — a comment costs more to leave than
#: a like, so it counts for more. Imported rather than redefined would be
#: a circular import; they are deliberately kept identical.
LIKE_WEIGHT = 1.0
COMMENT_WEIGHT = 3.0


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
    sort: str = "recent",
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> FeedRead:
    """Every post carrying this tag that the viewer may see.

    ``sort="top"`` puts the most-engaged first — what a hashtag page is
    FOR. Arriving on #pokemon and seeing whatever was posted ninety seconds
    ago tells you nothing about the tag; the best of it does. ``recent`` is
    the second tab, for people watching a tag live.
    """
    needle = normalise(tag)
    stmt = base_post_query(viewer.id).where(
        SocialPost.id.in_(
            select(SocialPostHashtag.post_id).where(SocialPostHashtag.tag == needle)
        )
    )

    if sort == "top":
        return await _top_page(db, viewer, stmt, cursor, limit)

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


async def _top_page(
    db: AsyncSession, viewer: User, stmt, cursor: str | None, limit: int
) -> FeedRead:
    """Most-engaged first. Offset-paged, like For You: the sort key is a
    computed score, so there is no column value to seek from."""
    offset = decode_offset(cursor)
    likes = (
        select(SocialPostLike.post_id, func.count().label("n"))
        .group_by(SocialPostLike.post_id)
        .subquery()
    )
    comments = (
        select(SocialPostComment.post_id, func.count().label("n"))
        .group_by(SocialPostComment.post_id)
        .subquery()
    )
    score = LIKE_WEIGHT * func.coalesce(likes.c.n, 0) + COMMENT_WEIGHT * func.coalesce(
        comments.c.n, 0
    )
    rows = (
        (
            await db.execute(
                stmt.outerjoin(likes, likes.c.post_id == SocialPost.id)
                .outerjoin(comments, comments.c.post_id == SocialPost.id)
                .order_by(score.desc(), SocialPost.created_at.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .unique()
        .all()
    )
    has_more = len(rows) > limit
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]
    return FeedRead(
        items=await post_payloads(db, viewer, page),
        next_cursor=encode_offset(offset + limit) if has_more else None,
    )


__all__ = ["normalise", "search", "tag_feed", "trending"]
