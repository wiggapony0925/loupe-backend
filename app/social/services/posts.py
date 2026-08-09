"""Posts: writing them, removing them, liking them, and the three feeds.

The feeds are defined HERE, not in the clients. What "Following" contains,
how "For You" ranks, which posts a private account exposes — those are
product decisions, and the moment two clients each hold their own copy they
start to disagree. Clients ask for a tab and render what comes back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user
from app.models.card import Card
from app.models.user import User
from app.social import hashtag_policy, post_media
from app.social.models import (
    SocialFollow,
    SocialModerationCase,
    SocialPost,
    SocialPostComment,
    SocialPostHashtag,
    SocialPostLike,
    SocialPostMedia,
    SocialPostMention,
    SocialProfile,
)
from app.social.schemas import (
    MAX_POST_BODY,
    FeedRead,
    PostLikeRead,
    PostRead,
)
from app.social.services import feed_notify, safety
from app.social.services._common import get_profile, resolve_username
from app.social.services.feed_common import (
    DEFAULT_PAGE,
    base_post_query,
    decode_cursor,
    decode_offset,
    discoverable_posts_predicate,
    encode_cursor,
    encode_offset,
    extract_hashtags,
    extract_mention_handles,
    post_payloads,
    resolve_mentions,
)

#: How far back "For You" looks. A discovery feed that can reach months into
#: the past stops being discovery and becomes an archive — and the ranking
#: query stays bounded no matter how large the table grows.
FORYOU_WINDOW = timedelta(days=30)

#: How many recent posts the ranker considers before taking the page. Large
#: enough that a good post from a quiet day still surfaces, small enough
#: that the group-by is a fixed cost.
FORYOU_POOL = 500

#: Engagement weights for the "For You" score. A comment is worth more than
#: a like because it costs more to leave.
LIKE_WEIGHT = 1.0
COMMENT_WEIGHT = 3.0

#: Hours of half-life on the recency term. Roughly a day and a half, so
#: yesterday's good post still competes with this morning's average one.
DECAY_HOURS = 36.0


@dataclass(frozen=True)
class NewImage:
    """One uploaded image, already validated by the router."""

    body: bytes
    content_type: str


async def create_post(
    db: AsyncSession,
    author: User,
    *,
    body: str | None,
    images: list[NewImage],
    card_id: uuid.UUID | None = None,
) -> PostRead:
    """Publish a post. Requires a claimed handle and some actual content."""
    profile = await get_profile(db, author.id)
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail="Claim a username before posting",
        )

    text = (body or "").strip()
    if len(text) > MAX_POST_BODY:
        raise HTTPException(
            status_code=422, detail=f"Caption is longer than {MAX_POST_BODY} characters"
        )
    if not text and not images and card_id is None:
        raise HTTPException(status_code=422, detail="A post needs a caption or a photo")
    if len(images) > post_media.MAX_IMAGES_PER_POST:
        raise HTTPException(
            status_code=422,
            detail=f"At most {post_media.MAX_IMAGES_PER_POST} photos per post",
        )
    if card_id is not None and await db.get(Card, card_id) is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # SCREENED BEFORE ANYTHING IS WRITTEN — text and photos together, in one
    # call through the shared chokepoint. Blocked content never becomes a row
    # or an object in the bucket; a 422 is raised from inside enforce().
    # `target_id` is a fresh uuid because nothing is stored yet: for a
    # refusal the case IS the record.
    provisional_id = uuid.uuid4()
    verdict = await safety.enforce(
        db,
        actor=author,
        surface=safety.TARGET_POST,
        target_id=provisional_id,
        text=text,
        images=[(image.body, image.content_type) for image in images],
    )

    post = SocialPost(author_id=author.id, body=text or None, card_id=card_id)
    db.add(post)
    await db.flush()

    # Images are written to the blob store BEFORE the row is committed. The
    # failure mode that leaves is an orphaned object nobody references —
    # wasted bytes. The other order would leave a post whose image 404s,
    # which the user sees.
    for position, image in enumerate(images):
        media_id = uuid.uuid4()
        key = await post_media.store(media_id, image.body, image.content_type)
        width, height = post_media.probe_size(image.body)
        db.add(
            SocialPostMedia(
                id=media_id,
                post_id=post.id,
                position=position,
                storage_key=key,
                content_type=image.content_type,
                width=width,
                height=height,
            )
        )

    # Only tags that earn a page get a row. A blocked tag is NOT censored
    # out of the caption — the words stay the author's — it simply doesn't
    # become a browsable page, a trending chip or a suggestion. The client
    # stops linking it for free, because PostCaption only styles tags the
    # server returned. See app/social/hashtag_policy.py.
    for tag in hashtag_policy.indexable(extract_hashtags(text)):
        db.add(SocialPostHashtag(post_id=post.id, tag=tag))

    mentioned = await resolve_mentions(db, extract_mention_handles(text))
    for user_id in mentioned.values():
        if user_id != author.id:
            db.add(SocialPostMention(post_id=post.id, user_id=user_id))

    if verdict.needs_review:
        # enforce() already opened the case against the provisional id (it
        # had to — the row didn't exist yet). Point it at the real post now
        # that there is one, so a moderator can open what they're judging.
        await _retarget_case(db, provisional_id, post.id)

    await db.commit()
    await db.refresh(post)

    # Mentions first: being named in a post is more specific than "someone
    # you follow posted", and the dedupe keys differ so a mentioned follower
    # gets both — which is right, they're different facts.
    await feed_notify.mentioned_in_post(
        db,
        actor=author,
        actor_profile=profile,
        post=post,
        user_ids=[uid for uid in mentioned.values() if uid != author.id],
    )
    await feed_notify.posted(db, author=author, author_profile=profile, post=post)
    return await get_post(db, author, post.id)


async def _retarget_case(
    db: AsyncSession, provisional_id: uuid.UUID, post_id: uuid.UUID
) -> None:
    """Repoint the case enforce() opened before the post row existed."""
    await db.execute(
        update(SocialModerationCase)
        .where(SocialModerationCase.target_id == provisional_id)
        .values(target_id=post_id)
    )


async def edit_post(
    db: AsyncSession,
    viewer: User,
    post_id: uuid.UUID,
    *,
    body: str | None,
) -> PostRead:
    """Rewrite a post's caption. Author only — staff can delete, not rewrite.

    **The caption only.** Photos are immutable once published, which is
    Instagram's rule and the right one: swapping the image under a post
    people have already liked and commented on changes what they endorsed.
    Editing words is a correction; editing the picture is a bait-and-switch.

    A new caption is NEW USER CONTENT, so it goes through the same
    moderation chokepoint as the original — otherwise "post something
    innocent, then edit it" is an open door straight past the screen.
    Hashtags and mentions are re-derived rather than patched, because a
    diff would have to be exactly right about removals to avoid leaving a
    post indexed under a tag its caption no longer contains.
    """
    post = await db.get(SocialPost, post_id)
    if post is None or post.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Post not found")
    # Not `is_admin_user`: a moderator's tools are remove and resolve. An
    # admin silently rewriting someone's words, still bylined to them, is
    # not moderation.
    if post.author_id != viewer.id:
        raise HTTPException(status_code=403, detail="Not your post")

    text = (body or "").strip()
    if len(text) > MAX_POST_BODY:
        raise HTTPException(
            status_code=422, detail=f"Caption is longer than {MAX_POST_BODY} characters"
        )

    # A post is allowed to have no caption, but only if something else
    # carries it. Clearing the text of a text-only post is a delete, and
    # should be done as one so the author knows that's what happened.
    if not text:
        has_media = (
            await db.execute(
                select(func.count())
                .select_from(SocialPostMedia)
                .where(SocialPostMedia.post_id == post.id)
            )
        ).scalar_one()
        if not has_media and post.card_id is None:
            raise HTTPException(
                status_code=422,
                detail="A post needs a caption or a photo — delete it instead",
            )

    if text == (post.body or ""):
        # Nothing changed. Don't stamp `edited_at` for a no-op save, or
        # opening the editor and hitting Save marks the post as rewritten.
        return await get_post(db, viewer, post_id)

    verdict = await safety.enforce(
        db,
        actor=viewer,
        surface=safety.TARGET_POST,
        target_id=post.id,
        text=text,
    )

    post.body = text or None
    post.edited_at = datetime.now(UTC)

    # Re-index from scratch: delete then re-derive. See the docstring —
    # patching the difference is where a stale tag row survives.
    await db.execute(
        delete(SocialPostHashtag).where(SocialPostHashtag.post_id == post.id)
    )
    for tag in hashtag_policy.indexable(extract_hashtags(text)):
        db.add(SocialPostHashtag(post_id=post.id, tag=tag))

    # Mentions added by an edit notify; ones already there don't re-notify,
    # and ones removed lose the row but keep the notification already sent —
    # that notification was true when it fired.
    existing = set(
        (
            await db.execute(
                select(SocialPostMention.user_id).where(
                    SocialPostMention.post_id == post.id
                )
            )
        )
        .scalars()
        .all()
    )
    mentioned = await resolve_mentions(db, extract_mention_handles(text))
    keep = {uid for uid in mentioned.values() if uid != viewer.id}
    await db.execute(
        delete(SocialPostMention).where(
            SocialPostMention.post_id == post.id,
            SocialPostMention.user_id.notin_(keep or [uuid.uuid4()]),
        )
    )
    for user_id in sorted(keep - existing, key=str):
        db.add(SocialPostMention(post_id=post.id, user_id=user_id))

    await db.commit()

    newly_mentioned = keep - existing
    if newly_mentioned:
        profile = await get_profile(db, viewer.id)
        if profile is not None:
            await feed_notify.mentioned_in_post(
                db,
                actor=viewer,
                actor_profile=profile,
                post=post,
                user_ids=sorted(newly_mentioned, key=str),
            )

    # enforce() has already opened the case against the real post id (unlike
    # create_post, there is a row to point at from the start), so a flagged
    # edit needs nothing further here.
    del verdict

    return await get_post(db, viewer, post_id)


async def delete_post(db: AsyncSession, viewer: User, post_id: uuid.UUID) -> None:
    """Soft-delete. Author or staff only.

    Soft rather than hard so the comment thread other people wrote isn't
    collateral damage, and so a moderation decision stays auditable.
    """
    post = await db.get(SocialPost, post_id)
    if post is None or post.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.author_id != viewer.id and not is_admin_user(viewer):
        raise HTTPException(status_code=403, detail="Not your post")
    post.deleted_at = datetime.now(UTC)
    await db.commit()


async def get_post(db: AsyncSession, viewer: User, post_id: uuid.UUID) -> PostRead:
    """One post, privacy-gated — the permalink and the post-detail screen."""
    row = (
        await db.execute(base_post_query(viewer.id).where(SocialPost.id == post_id))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found")
    payloads = await post_payloads(db, viewer, [(row[0], row[1], row[2])])
    return payloads[0]


# ── Feeds ──


async def feed(
    db: AsyncSession,
    viewer: User,
    *,
    tab: str,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> FeedRead:
    if tab == "following":
        return await _following_feed(db, viewer, cursor, limit)
    if tab == "mine":
        return await _author_feed(db, viewer, viewer.id, cursor, limit)
    if tab == "foryou":
        return await _foryou_feed(db, viewer, cursor, limit)
    raise HTTPException(status_code=422, detail=f"Unknown feed tab '{tab}'")


async def _following_feed(
    db: AsyncSession, viewer: User, cursor: str | None, limit: int
) -> FeedRead:
    """People you follow, plus yourself, newest first.

    Your own posts belong here: a brand-new account that follows nobody
    would otherwise land on an empty screen straight after posting and
    conclude the post failed.
    """
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer.id
    )
    stmt = base_post_query(viewer.id).where(
        SocialPost.author_id.in_(followed) | (SocialPost.author_id == viewer.id)
    )
    return await _keyset_page(db, viewer, stmt, cursor, limit)


async def _author_feed(
    db: AsyncSession,
    viewer: User,
    author_id: uuid.UUID,
    cursor: str | None,
    limit: int,
) -> FeedRead:
    stmt = base_post_query(viewer.id).where(SocialPost.author_id == author_id)
    return await _keyset_page(db, viewer, stmt, cursor, limit)


async def user_feed(
    db: AsyncSession,
    viewer: User,
    username: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE,
) -> FeedRead:
    """A collector's own posts — the grid on their profile.

    The privacy gate is the shared predicate, so a private account the
    viewer doesn't follow simply yields nothing rather than a 403: the
    profile page already says the account is private, and a second error
    for the same fact just makes the page look broken.
    """
    profile, _ = await resolve_username(db, username, viewer)
    return await _author_feed(db, viewer, profile.user_id, cursor, limit)


async def _keyset_page(
    db: AsyncSession, viewer: User, stmt, cursor: str | None, limit: int
) -> FeedRead:
    seek = decode_cursor(cursor)
    if seek is not None:
        created_at, last_id = seek
        # Tuple comparison, so posts sharing a timestamp still page cleanly.
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
    next_cursor = (
        encode_cursor(page[-1][0].created_at, page[-1][0].id)
        if has_more and page
        else None
    )
    return FeedRead(items=items, next_cursor=next_cursor)


async def _foryou_feed(
    db: AsyncSession, viewer: User, cursor: str | None, limit: int
) -> FeedRead:
    """Discovery: recent posts ranked by engagement, decayed by age.

    Paged by OFFSET rather than a keyset, because the sort key is a computed
    score with no column to seek from. That's safe here where a keyset would
    be needed elsewhere: the ranking is over a fixed recent window, so the
    ordering a reader is paging through barely moves under them.

    Posts the viewer already follows are NOT excluded — someone you follow
    going viral is exactly what a discovery feed should show you — but your
    own posts are, because being recommended your own photos is absurd.

    Private authors are excluded outright, even ones the viewer follows.
    See :func:`discoverable_posts_predicate`: "can they see it" and "may we
    recommend it" are different questions, and this feed asks the second.
    """
    offset = decode_offset(cursor)
    since = datetime.now(UTC) - FORYOU_WINDOW

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

    age_hours = func.extract("epoch", func.now() - SocialPost.created_at) / 3600.0
    # Engagement over a decaying denominator — Hacker News' shape. Ordering
    # by raw counts alone would freeze the feed on whatever went viral once.
    score = (
        1.0
        + LIKE_WEIGHT * func.coalesce(likes.c.n, 0)
        + COMMENT_WEIGHT * func.coalesce(comments.c.n, 0)
    ) / func.power(1.0 + age_hours / DECAY_HOURS, 1.5)

    stmt = (
        select(SocialPost, SocialProfile, User)
        .join(SocialProfile, SocialProfile.user_id == SocialPost.author_id)
        .join(User, User.id == SocialPost.author_id)
        .outerjoin(likes, likes.c.post_id == SocialPost.id)
        .outerjoin(comments, comments.c.post_id == SocialPost.id)
        .where(
            User.deleted_at.is_(None),
            User.banned_at.is_(None),
            SocialPost.author_id != viewer.id,
            SocialPost.created_at >= since,
            discoverable_posts_predicate(),
        )
        .order_by(score.desc(), SocialPost.created_at.desc(), SocialPost.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    rows = (await db.execute(stmt)).unique().all()

    has_more = len(rows) > limit and offset + limit < FORYOU_POOL
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]
    items = await post_payloads(db, viewer, page)
    return FeedRead(
        items=items,
        next_cursor=encode_offset(offset + limit) if has_more else None,
    )


# ── Likes ──


async def like_post(db: AsyncSession, viewer: User, post_id: uuid.UUID) -> PostLikeRead:
    """Idempotent — liking twice is still one like."""
    post = await _visible_post(db, viewer, post_id)
    existing = await db.get(SocialPostLike, (viewer.id, post_id))
    if existing is None:
        db.add(SocialPostLike(user_id=viewer.id, post_id=post_id))
        await db.commit()
        await feed_notify.post_liked(db, actor=viewer, post=post)
    return PostLikeRead(liked=True, like_count=await _like_count(db, post_id))


async def unlike_post(
    db: AsyncSession, viewer: User, post_id: uuid.UUID
) -> PostLikeRead:
    await _visible_post(db, viewer, post_id)
    existing = await db.get(SocialPostLike, (viewer.id, post_id))
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    return PostLikeRead(liked=False, like_count=await _like_count(db, post_id))


async def _like_count(db: AsyncSession, post_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialPostLike)
                .where(SocialPostLike.post_id == post_id)
            )
        ).scalar_one()
        or 0
    )


async def _visible_post(
    db: AsyncSession, viewer: User, post_id: uuid.UUID
) -> SocialPost:
    """The post, if this viewer is allowed to interact with it.

    404 rather than 403 for a private account's post: distinguishing the two
    would confirm the post exists, which is the thing being kept private.
    """
    row = (
        await db.execute(base_post_query(viewer.id).where(SocialPost.id == post_id))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return row[0]


__all__ = [
    "NewImage",
    "create_post",
    "delete_post",
    "edit_post",
    "feed",
    "get_post",
    "like_post",
    "unlike_post",
    "user_feed",
]
