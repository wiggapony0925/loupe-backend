"""Feed events → the notification inbox (and the phone).

This module COMPOSES: it works out who should hear about a feed event and
with what parameters. The wording, category, deep link, dedupe key and
push policy all live in :mod:`app.services.notification_templates` — one
catalog for every notification the product sends, so the inbox row and the
push can never drift.

Two rules, both learned from the shape of :mod:`app.services.notification_service`:

* **Never notify yourself.** Liking your own post must not light your bell.
* **Never let delivery break the action.** A like that 500s because the
  inbox write failed is a much worse bug than a missing notification, so
  everything here is best-effort and swallows its own errors.

And one scoping rule that is the whole design: **nothing here broadcasts.**
A new post notifies the author's FOLLOWERS (capped); everything else goes
to exactly the person acted upon. If a notification kind ever needs to
reach "everyone", it does not belong in this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.user import User
from app.services import notification_templates
from app.social.models import (
    SocialFollow,
    SocialPost,
    SocialPostComment,
    SocialPostMedia,
    SocialProfile,
)
from app.utils.logger import get_logger

logger = get_logger("social.feed.notify")


#: Deep link a tapped notification opens. A PATH, not a URL — web and native
#: each resolve it with their own router (see Notification.href).
def post_href(post_id: uuid.UUID) -> str:
    return f"/app/community/p/{post_id}"


def _preview(text: str | None, limit: int = 80) -> str | None:
    """A comment's opening words, for the notification body."""
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _name(profile: SocialProfile, user: User) -> str:
    return user.display_name or f"@{profile.username}"


async def post_liked(db: AsyncSession, *, actor: User, post: SocialPost) -> None:
    """ "@x liked your post"."""
    if post.author_id == actor.id:
        return
    profile = await _profile(db, actor.id)
    if profile is None:
        return
    await _send(
        db,
        post.author_id,
        "social_post_like",
        actor=_name(profile, actor),
        actor_id=actor.id,
        post_id=post.id,
        preview=_preview(post.body),
        data={"post_id": str(post.id), "actor_id": str(actor.id)},
    )


async def commented(
    db: AsyncSession,
    *,
    actor: User,
    actor_profile: SocialProfile,
    post: SocialPost,
    comment: SocialPostComment,
    replied_to: SocialPostComment | None,
    mentioned_user_ids: Sequence[uuid.UUID],
) -> None:
    """Tell the post's author, the person replied to, and anyone named.

    Deliberately at most ONE notification per person per comment: replying
    to a comment on your own post while naming yourself should not produce
    three. ``told`` tracks who has already been covered.
    """
    told: set[uuid.UUID] = {actor.id}
    shared: dict[str, Any] = {
        "actor": _name(actor_profile, actor),
        "post_id": post.id,
        "comment_id": comment.id,
        "preview": _preview(comment.body),
    }
    payload = {"post_id": str(post.id), "comment_id": str(comment.id)}

    if replied_to is not None and replied_to.author_id not in told:
        told.add(replied_to.author_id)
        await _send(
            db, replied_to.author_id, "social_comment_reply", data=payload, **shared
        )

    if post.author_id not in told:
        told.add(post.author_id)
        await _send(db, post.author_id, "social_post_comment", data=payload, **shared)

    for user_id in mentioned_user_ids:
        if user_id in told:
            continue
        told.add(user_id)
        await _send(db, user_id, "social_mention_comment", data=payload, **shared)


async def mentioned_in_post(
    db: AsyncSession,
    *,
    actor: User,
    actor_profile: SocialProfile,
    post: SocialPost,
    user_ids: Sequence[uuid.UUID],
) -> None:
    """ "@x mentioned you in a post"."""
    who = _name(actor_profile, actor)
    for user_id in user_ids:
        if user_id == actor.id:
            continue
        await _send(
            db,
            user_id,
            "social_mention_post",
            actor=who,
            post_id=post.id,
            preview=_preview(post.body),
            data={"post_id": str(post.id), "actor_id": str(actor.id)},
        )


#: How many followers a single post notifies. A cap, not a policy: past
#: this, fanning out inline would make posting slow for the author and
#: bury everyone else's inbox. Beyond it the feed itself is the delivery
#: mechanism — which is what a feed is for.
MAX_POST_FANOUT = 500


async def posted(
    db: AsyncSession,
    *,
    author: User,
    author_profile: SocialProfile,
    post: SocialPost,
) -> None:
    """ "@x posted" — to the author's FOLLOWERS, and nobody else.

    The recipient list is the follow graph, full stop: someone who does not
    follow the author must never hear about the post. The summary is the
    caption's opening words, or a description of what the post actually is
    when there's no caption — a notification reading just "new post" tells
    you nothing about whether to open it.
    """
    follower_ids = [
        row[0]
        for row in (
            await db.execute(
                select(SocialFollow.follower_id)
                .where(SocialFollow.followee_id == author.id)
                .limit(MAX_POST_FANOUT)
            )
        ).all()
    ]
    if not follower_ids:
        return

    who = _name(author_profile, author)
    summary = _preview(post.body, limit=120) or await _describe(db, post)
    payload = {"post_id": str(post.id), "actor_id": str(author.id)}
    for user_id in follower_ids:
        if user_id == author.id:
            continue
        await _send(
            db,
            user_id,
            "social_new_post",
            actor=who,
            post_id=post.id,
            summary=summary,
            data=payload,
        )


async def _describe(db: AsyncSession, post: SocialPost) -> str:
    """What a captionless post IS, so the line still says something."""
    photos = int(
        (
            await db.execute(
                select(func.count())
                .select_from(SocialPostMedia)
                .where(SocialPostMedia.post_id == post.id)
            )
        ).scalar_one()
        or 0
    )
    if post.card_id is not None:
        card = await db.get(Card, post.card_id)
        if card is not None and card.name:
            return f"Showed off {card.name}"
    if photos == 1:
        return "Shared a photo"
    if photos > 1:
        return f"Shared {photos} photos"
    return "Shared a post"


async def followed(db: AsyncSession, *, actor: User, target_id: uuid.UUID) -> None:
    """ "@x started following you" — the bell's most common entry."""
    if target_id == actor.id:
        return
    profile = await _profile(db, actor.id)
    if profile is None:
        return
    await _send(
        db,
        target_id,
        "social_follow",
        actor=_name(profile, actor),
        actor_id=actor.id,
        actor_username=profile.username,
        data={"actor_id": str(actor.id), "username": profile.username},
    )


async def follow_requested(
    db: AsyncSession, *, actor: User, target_id: uuid.UUID
) -> None:
    """ "@x wants to follow you" — a private account's pending request.

    Without this the request sat silently in the inbox page until the owner
    happened to visit it. (A dead twin of this function existed for months
    in ``social_notify.py`` — written, never called.)
    """
    if target_id == actor.id:
        return
    profile = await _profile(db, actor.id)
    if profile is None:
        return
    await _send(
        db,
        target_id,
        "social_follow_request",
        actor=_name(profile, actor),
        actor_id=actor.id,
        data={"actor_id": str(actor.id), "username": profile.username},
    )


async def follow_request_accepted(
    db: AsyncSession, *, owner: User, requester_id: uuid.UUID
) -> None:
    """ "@x accepted your follow request" — tell the person who asked."""
    if requester_id == owner.id:
        return
    profile = await _profile(db, owner.id)
    if profile is None:
        return
    await _send(
        db,
        requester_id,
        "social_follow_accepted",
        actor=_name(profile, owner),
        actor_id=owner.id,
        actor_username=profile.username,
        data={"actor_id": str(owner.id), "username": profile.username},
    )


async def _profile(db: AsyncSession, user_id: uuid.UUID) -> SocialProfile | None:
    return (
        await db.execute(select(SocialProfile).where(SocialProfile.user_id == user_id))
    ).scalar_one_or_none()


async def _send(
    db: AsyncSession,
    user_id: uuid.UUID,
    template_id: str,
    *,
    data: dict[str, Any],
    **params: Any,
) -> None:
    """Best-effort delivery — a failed notification never fails the action."""
    try:
        await notification_templates.send(db, user_id, template_id, data=data, **params)
    except Exception:
        logger.exception("social notification failed user=%s", user_id)
        await db.rollback()


__all__ = [
    "MAX_POST_FANOUT",
    "commented",
    "follow_request_accepted",
    "follow_requested",
    "followed",
    "mentioned_in_post",
    "post_href",
    "post_liked",
    "posted",
]
