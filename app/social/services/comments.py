"""Comment threads: one level of replies, each with its own likes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user
from app.models.user import User
from app.social import moderation
from app.social.models import (
    SocialCommentLike,
    SocialPost,
    SocialPostComment,
    SocialProfile,
)
from app.social.schemas import (
    MAX_COMMENT_BODY,
    CommentRead,
    CommentThreadRead,
    PostLikeRead,
)
from app.social.services import feed_notify, safety
from app.social.services.feed_common import (
    DEFAULT_PAGE,
    INLINE_REPLIES,
    author_card,
    base_post_query,
    extract_mention_handles,
    relationships_to,
    resolve_mentions,
)


async def list_comments(
    db: AsyncSession,
    viewer: User,
    post_id: uuid.UUID,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
) -> CommentThreadRead:
    """Top-level comments OLDEST first, each with its first few replies.

    Oldest-first because a comment thread is a conversation and reads down;
    the feed above it is newest-first because that is a river. Reversing
    either one makes it unreadable.

    Offset paging is right here where it was wrong for the feed: a thread
    grows at the BOTTOM, so rows above the reader's position don't shift.
    """
    await _visible_post(db, viewer, post_id)

    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialPostComment)
                .where(SocialPostComment.post_id == post_id)
            )
        ).scalar_one()
        or 0
    )

    rows = (
        await db.execute(
            _comment_query()
            .where(
                SocialPostComment.post_id == post_id,
                SocialPostComment.parent_id.is_(None),
            )
            .order_by(SocialPostComment.created_at.asc(), SocialPostComment.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]

    parent_ids = [c.id for c, _, _ in page]
    reply_rows = await _inline_replies(db, parent_ids)
    reply_counts = await _reply_counts(db, parent_ids)

    all_rows = page + [r for group in reply_rows.values() for r in group]
    likes, mine = await _like_state(db, viewer.id, [c.id for c, _, _ in all_rows])
    rels = await relationships_to(db, viewer.id, [c.author_id for c, _, _ in all_rows])
    staff = is_admin_user(viewer)

    def render(row: tuple[SocialPostComment, SocialProfile, User]) -> CommentRead:
        comment, profile, account = row
        return CommentRead(
            id=comment.id,
            post_id=comment.post_id,
            parent_id=comment.parent_id,
            author=author_card(profile, account, rels.get(comment.author_id, "none")),
            body=comment.body,
            created_at=comment.created_at,
            like_count=likes.get(comment.id, 0),
            viewer_has_liked=comment.id in mine,
            reply_count=reply_counts.get(comment.id, 0),
            replies=[render(r) for r in reply_rows.get(comment.id, [])],
            can_delete=staff or comment.author_id == viewer.id,
        )

    return CommentThreadRead(
        items=[render(row) for row in page],
        next_cursor=str(offset + limit) if has_more else None,
        total=total,
    )


async def list_replies(
    db: AsyncSession,
    viewer: User,
    comment_id: uuid.UUID,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
) -> CommentThreadRead:
    """The full reply list behind "View N replies"."""
    parent = await db.get(SocialPostComment, comment_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    await _visible_post(db, viewer, parent.post_id)

    rows = (
        await db.execute(
            _comment_query()
            .where(SocialPostComment.parent_id == comment_id)
            .order_by(SocialPostComment.created_at.asc(), SocialPostComment.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]

    likes, mine = await _like_state(db, viewer.id, [c.id for c, _, _ in page])
    rels = await relationships_to(db, viewer.id, [c.author_id for c, _, _ in page])
    staff = is_admin_user(viewer)
    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialPostComment)
                .where(SocialPostComment.parent_id == comment_id)
            )
        ).scalar_one()
        or 0
    )
    return CommentThreadRead(
        items=[
            CommentRead(
                id=comment.id,
                post_id=comment.post_id,
                parent_id=comment.parent_id,
                author=author_card(
                    profile, account, rels.get(comment.author_id, "none")
                ),
                body=comment.body,
                created_at=comment.created_at,
                like_count=likes.get(comment.id, 0),
                viewer_has_liked=comment.id in mine,
                can_delete=staff or comment.author_id == viewer.id,
            )
            for comment, profile, account in page
        ],
        next_cursor=str(offset + limit) if has_more else None,
        total=total,
    )


async def add_comment(
    db: AsyncSession,
    author: User,
    post_id: uuid.UUID,
    *,
    body: str,
    parent_id: uuid.UUID | None = None,
) -> CommentRead:
    """Comment, or reply. Replies to replies attach to the top-level parent."""
    post = await _visible_post(db, author, post_id)
    profile = (
        await db.execute(
            select(SocialProfile).where(SocialProfile.user_id == author.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=409, detail="Claim a username before commenting"
        )

    text = body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Comment can't be empty")
    if len(text) > MAX_COMMENT_BODY:
        raise HTTPException(
            status_code=422,
            detail=f"Comment is longer than {MAX_COMMENT_BODY} characters",
        )

    # Comments are text-only, so screening is a cheap single call. Same
    # policy as posts: refuse the zero-tolerance set, publish-and-queue the
    # rest (see app/social/moderation.py).
    verdict = await moderation.screen(text)
    if verdict.blocked:
        await safety.open_auto_case(
            db,
            target_type=safety.TARGET_COMMENT,
            target_id=uuid.uuid4(),
            author_id=author.id,
            verdict=verdict,
            excerpt=text,
        )
        await db.commit()
        raise HTTPException(status_code=422, detail=verdict.message())

    replied_to: SocialPostComment | None = None
    if parent_id is not None:
        replied_to = await db.get(SocialPostComment, parent_id)
        if replied_to is None or replied_to.post_id != post_id:
            raise HTTPException(status_code=404, detail="Comment not found")
        # Flatten: threading stays one level deep (see the model docstring).
        parent_id = replied_to.parent_id or replied_to.id

    comment = SocialPostComment(
        post_id=post_id, author_id=author.id, parent_id=parent_id, body=text
    )
    db.add(comment)
    await db.flush()
    if verdict.needs_review:
        await safety.open_auto_case(
            db,
            target_type=safety.TARGET_COMMENT,
            target_id=comment.id,
            author_id=author.id,
            verdict=verdict,
            excerpt=text,
        )
    await db.commit()
    await db.refresh(comment)

    mentioned = await resolve_mentions(db, extract_mention_handles(text))
    await feed_notify.commented(
        db,
        actor=author,
        actor_profile=profile,
        post=post,
        comment=comment,
        replied_to=replied_to,
        mentioned_user_ids=[uid for uid in mentioned.values() if uid != author.id],
    )

    return CommentRead(
        id=comment.id,
        post_id=comment.post_id,
        parent_id=comment.parent_id,
        author=author_card(profile, author, "self"),
        body=comment.body,
        created_at=comment.created_at,
        can_delete=True,
    )


async def delete_comment(db: AsyncSession, viewer: User, comment_id: uuid.UUID) -> None:
    """The comment's author, the POST's author, or staff may remove it.

    The post's author is included on purpose: someone has to be able to
    clear abuse off their own post without waiting for moderation.
    """
    comment = await db.get(SocialPostComment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    post = await db.get(SocialPost, comment.post_id)
    owner = post is not None and post.author_id == viewer.id
    if comment.author_id != viewer.id and not owner and not is_admin_user(viewer):
        raise HTTPException(status_code=403, detail="Not your comment")
    # Hard delete: unlike a post, a removed comment has no thread hanging off
    # it worth preserving, and CASCADE takes its replies and likes with it.
    await db.delete(comment)
    await db.commit()


async def like_comment(
    db: AsyncSession, viewer: User, comment_id: uuid.UUID, *, liked: bool
) -> PostLikeRead:
    """Set the heart on a comment. Idempotent in both directions."""
    comment = await db.get(SocialPostComment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    await _visible_post(db, viewer, comment.post_id)

    existing = await db.get(SocialCommentLike, (viewer.id, comment_id))
    if liked and existing is None:
        db.add(SocialCommentLike(user_id=viewer.id, comment_id=comment_id))
        await db.commit()
    elif not liked and existing is not None:
        await db.delete(existing)
        await db.commit()

    total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialCommentLike)
                .where(SocialCommentLike.comment_id == comment_id)
            )
        ).scalar_one()
        or 0
    )
    return PostLikeRead(liked=liked, like_count=total)


# ── internals ──


def _comment_query():
    return (
        select(SocialPostComment, SocialProfile, User)
        .join(SocialProfile, SocialProfile.user_id == SocialPostComment.author_id)
        .join(User, User.id == SocialPostComment.author_id)
        .where(User.deleted_at.is_(None), User.banned_at.is_(None))
    )


async def _visible_post(
    db: AsyncSession, viewer: User, post_id: uuid.UUID
) -> SocialPost:
    row = (
        await db.execute(base_post_query(viewer.id).where(SocialPost.id == post_id))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return row[0]


async def _inline_replies(
    db: AsyncSession, parent_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[SocialPostComment, SocialProfile, User]]]:
    """First :data:`INLINE_REPLIES` replies for MANY parents in one query.

    A window function rather than a query per parent: a thread of 20
    comments would otherwise cost 20 extra round trips to show two replies
    each.
    """
    if not parent_ids:
        return {}
    ranked = (
        select(
            SocialPostComment.id.label("cid"),
            func.row_number()
            .over(
                partition_by=SocialPostComment.parent_id,
                order_by=(
                    SocialPostComment.created_at.asc(),
                    SocialPostComment.id.asc(),
                ),
            )
            .label("rank"),
        )
        .where(SocialPostComment.parent_id.in_(list(parent_ids)))
        .subquery()
    )
    rows = (
        await db.execute(
            _comment_query()
            .join(ranked, ranked.c.cid == SocialPostComment.id)
            .where(ranked.c.rank <= INLINE_REPLIES)
            .order_by(SocialPostComment.created_at.asc())
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[SocialPostComment, SocialProfile, User]]] = {}
    for row in rows:
        comment = row[0]
        if comment.parent_id is not None:
            out.setdefault(comment.parent_id, []).append((row[0], row[1], row[2]))
    return out


async def _reply_counts(
    db: AsyncSession, parent_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not parent_ids:
        return {}
    return {
        pid: int(n)
        for pid, n in (
            await db.execute(
                select(SocialPostComment.parent_id, func.count())
                .where(SocialPostComment.parent_id.in_(list(parent_ids)))
                .group_by(SocialPostComment.parent_id)
            )
        ).all()
    }


async def _like_state(
    db: AsyncSession, viewer_id: uuid.UUID, comment_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], set[uuid.UUID]]:
    if not comment_ids:
        return {}, set()
    ids = list(comment_ids)
    counts = {
        cid: int(n)
        for cid, n in (
            await db.execute(
                select(SocialCommentLike.comment_id, func.count())
                .where(SocialCommentLike.comment_id.in_(ids))
                .group_by(SocialCommentLike.comment_id)
            )
        ).all()
    }
    mine = {
        row[0]
        for row in (
            await db.execute(
                select(SocialCommentLike.comment_id).where(
                    SocialCommentLike.user_id == viewer_id,
                    SocialCommentLike.comment_id.in_(ids),
                )
            )
        ).all()
    }
    return counts, mine


__all__ = [
    "add_comment",
    "delete_comment",
    "like_comment",
    "list_comments",
    "list_replies",
]
