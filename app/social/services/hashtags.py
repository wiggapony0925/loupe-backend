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
    discoverable_post_ids,
    discoverable_posts_predicate,
    encode_cursor,
    encode_offset,
    post_payloads,
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

    Counts only *discoverable* posts, so the chips agree with the page they
    open. Counting posts the viewer can merely see would inflate a tag with
    private-account posts and then land them on a page that doesn't show
    any of them — a chip promising 40 posts and delivering 3.
    """
    since = datetime.now(UTC) - TRENDING_WINDOW
    rows = (
        await db.execute(
            select(SocialPostHashtag.tag, func.count().label("n"))
            .select_from(SocialPostHashtag)
            .join(SocialPost, SocialPost.id == SocialPostHashtag.post_id)
            .where(
                SocialPost.id.in_(discoverable_post_ids()),
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
    """Tag typeahead — prefix matches first, then anything containing it.

    Scoped to discoverable posts for the same reason as :func:`trending`:
    every suggestion here is a link to a tag page.
    """
    needle = normalise(query)
    if not needle:
        return []
    rows = (
        await db.execute(
            select(SocialPostHashtag.tag, func.count().label("n"))
            .join(SocialPost, SocialPost.id == SocialPostHashtag.post_id)
            .where(
                SocialPost.id.in_(discoverable_post_ids()),
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


async def recent_for_author(
    db: AsyncSession, viewer: User, *, limit: int = 12
) -> list[HashtagRead]:
    """Tags this author has used before, most recently used first.

    The composer's suggestion row. Trending answers "what is the app
    talking about"; this answers "what do YOU tag things", which is the
    more useful question when the box is empty — people tag their own
    posts consistently (#psa10, #vintagebase) and retyping the same eight
    tags by hand is the friction worth removing.

    Backfilled with trending when someone hasn't posted enough to have a
    habit yet, so the row is never empty on a new account. Their own tags
    always rank above the borrowed ones.
    """
    mine = (
        await db.execute(
            select(
                SocialPostHashtag.tag,
                func.count().label("n"),
                func.max(SocialPost.created_at).label("last_used"),
            )
            .join(SocialPost, SocialPost.id == SocialPostHashtag.post_id)
            .where(
                SocialPost.author_id == viewer.id,
                SocialPost.deleted_at.is_(None),
            )
            .group_by(SocialPostHashtag.tag)
            .order_by(func.max(SocialPost.created_at).desc())
            .limit(limit)
        )
    ).all()

    out = [HashtagRead(tag=tag, post_count=int(n)) for tag, n, _ in mine]
    if len(out) >= limit:
        return out

    seen = {row.tag for row in out}
    for row in await trending(db, viewer, limit=limit - len(out) + len(seen)):
        if row.tag not in seen:
            out.append(row)
        if len(out) >= limit:
            break
    return out


async def tag_feed(
    db: AsyncSession,
    viewer: User,
    tag: str,
    *,
    sort: str = "recent",
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> FeedRead:
    """Every PUBLIC post carrying this tag.

    ``sort="top"`` puts the most-engaged first — what a hashtag page is
    FOR. Arriving on #pokemon and seeing whatever was posted ninety seconds
    ago tells you nothing about the tag; the best of it does. ``recent`` is
    the second tab, for people watching a tag live.

    Private authors never appear here, not even for their own followers.
    A tag page is how a stranger finds a post, so putting a private
    account's photo on one defeats the setting — and a page whose contents
    changed depending on who you follow would make "who can see this tag?"
    unanswerable for the person who wrote it.
    """
    needle = normalise(tag)
    stmt = base_post_query(viewer.id).where(
        discoverable_posts_predicate(),
        SocialPost.id.in_(
            select(SocialPostHashtag.post_id).where(SocialPostHashtag.tag == needle)
        ),
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
