"""Shared internals for the feed services (posts, comments, hashtags).

Three things live here because posts, comments and hashtag pages all need
them and none of them owns them:

* **Caption parsing** — one definition of what a ``#tag`` and an ``@handle``
  are. Two implementations would eventually disagree, and a caption whose
  tags render differently from the tags it was indexed under is a bug users
  can see.
* **Cursor tokens** — opaque keyset pagination.
* **Visibility** — the single answer to "whose posts may this viewer see",
  expressed as a SQL predicate so the privacy rule is applied by the
  database on every feed rather than re-checked per row by each caller.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user
from app.models.card import Card, CardSet
from app.models.user import User
from app.social import avatars, post_media
from app.social.models import (
    SocialFollow,
    SocialPost,
    SocialPostComment,
    SocialPostHashtag,
    SocialPostLike,
    SocialPostMedia,
    SocialPostMention,
    SocialProfile,
)
from app.social.schemas import (
    PostAuthor,
    PostCardRef,
    PostMediaRead,
    PostRead,
    RelationshipState,
)

#: A hashtag: '#', then letters/digits/underscore. Deliberately excludes '.'
#: and '-' so a caption ending "#psa10." doesn't index the full stop, and
#: bounded so a wall of '#' can't produce a 4 KB tag.
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{1,64})")

#: A mention: '@', then the handle grammar from USERNAME_PATTERN. Trailing
#: dots are trimmed by the resolver so "@ash." finds @ash.
MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._]{2,29})")

#: Feed page size. Twenty posts is roughly three screens of scrolling —
#: enough that the next page loads before the user reaches the end.
DEFAULT_PAGE = 20
MAX_PAGE = 50

#: How many replies ride along with a top-level comment. Instagram-style:
#: enough to read the gist, with "View N replies" for the rest.
INLINE_REPLIES = 2


# ── Caption parsing ──


def extract_hashtags(body: str | None) -> list[str]:
    """Lowercase, de-duplicated tags in first-appearance order."""
    if not body:
        return []
    seen: dict[str, None] = {}
    for match in HASHTAG_RE.findall(body):
        seen.setdefault(match.lower(), None)
    return list(seen)


def extract_mention_handles(body: str | None) -> list[str]:
    """Candidate handles in a caption — not yet known to exist.

    Trailing dots and underscores are stripped: the handle grammar allows
    them mid-word, so "thanks @ash." would otherwise look for "ash.".
    """
    if not body:
        return []
    seen: dict[str, None] = {}
    for match in MENTION_RE.findall(body):
        handle = match.lower().rstrip("._")
        if len(handle) >= 3:
            seen.setdefault(handle, None)
    return list(seen)


async def resolve_mentions(
    db: AsyncSession, handles: Sequence[str]
) -> dict[str, uuid.UUID]:
    """Map handles to user ids, dropping the ones nobody owns."""
    if not handles:
        return {}
    rows = (
        await db.execute(
            select(SocialProfile.username, SocialProfile.user_id)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialProfile.username.in_(list(handles)),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


# ── Cursors ──


def encode_cursor(created_at: datetime, post_id: uuid.UUID) -> str:
    """Opaque keyset token: the sort key of the last row on the page.

    Base64 so nobody is tempted to build one client-side — the encoding is
    the API's business and the shape is free to change.
    """
    raw = f"{created_at.isoformat()}|{post_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    """Inverse of :func:`encode_cursor`. 400s on a token we didn't issue."""
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


def encode_offset(offset: int) -> str:
    """Cursor for the ranked feed, where a keyset can't apply.

    "For You" is ordered by a score, not by a column, so there is no row
    value to seek from. The token still goes through the same opaque channel
    so clients treat every feed identically.
    """
    return base64.urlsafe_b64encode(f"o|{offset}".encode()).decode().rstrip("=")


def decode_offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        kind, _, value = raw.partition("|")
        if kind != "o":
            raise ValueError("wrong cursor kind")
        return max(0, int(value))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc


# ── Visibility ──


def visible_posts_predicate(viewer_id: uuid.UUID) -> ColumnElement[bool]:
    """Whose posts this viewer is allowed to see, as one SQL predicate.

    A post is visible when its author is public, is the viewer, or is
    someone the viewer already follows. Expressed as a predicate rather than
    a per-row check so every feed, tag page and permalink inherits the same
    rule — a privacy gate that has to be remembered at each call site is one
    that will eventually be forgotten at one of them.

    Authors with no claimed handle are excluded outright: they have no
    community presence, so their rows have nowhere to link to.
    """
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer_id
    )
    return and_(
        SocialPost.deleted_at.is_(None),
        SocialProfile.user_id.is_not(None),
        or_(
            SocialProfile.is_private.is_(False),
            SocialProfile.user_id == viewer_id,
            SocialProfile.user_id.in_(followed),
        ),
    )


def base_post_query(viewer_id: uuid.UUID) -> Select:
    """``SocialPost`` joined to its author, gated to what the viewer may see."""
    return (
        select(SocialPost, SocialProfile, User)
        .join(SocialProfile, SocialProfile.user_id == SocialPost.author_id)
        .join(User, User.id == SocialPost.author_id)
        .where(
            User.deleted_at.is_(None),
            User.banned_at.is_(None),
            visible_posts_predicate(viewer_id),
        )
    )


def visible_post_ids(viewer_id: uuid.UUID) -> Select:
    """Just the ids from :func:`base_post_query` — for ``IN`` sub-selects
    (trending counts, tag pages) where the author rows aren't needed."""
    return (
        select(SocialPost.id)
        .join(SocialProfile, SocialProfile.user_id == SocialPost.author_id)
        .join(User, User.id == SocialPost.author_id)
        .where(
            User.deleted_at.is_(None),
            User.banned_at.is_(None),
            visible_posts_predicate(viewer_id),
        )
    )


# ── Payload assembly ──


def author_card(
    profile: SocialProfile, user: User, relationship: RelationshipState
) -> PostAuthor:
    return PostAuthor(
        user_id=profile.user_id,
        username=profile.username,
        display_name=user.display_name,
        avatar_url=avatars.avatar_url(profile),
        is_pro=(user.plan or "").lower() == "pro",
        is_admin=is_admin_user(user),
        relationship=relationship,
    )


async def relationships_to(
    db: AsyncSession, viewer_id: uuid.UUID, user_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, RelationshipState]:
    """Viewer→author relationship for a whole page in ONE query.

    The alternative — ``relationship_between`` per row — is 20 round trips
    to draw 20 Follow buttons.
    """
    ids = [uid for uid in dict.fromkeys(user_ids) if uid != viewer_id]
    out: dict[uuid.UUID, RelationshipState] = {}
    if ids:
        followed = {
            row[0]
            for row in (
                await db.execute(
                    select(SocialFollow.followee_id).where(
                        SocialFollow.follower_id == viewer_id,
                        SocialFollow.followee_id.in_(ids),
                    )
                )
            ).all()
        }
        for uid in ids:
            out[uid] = "following" if uid in followed else "none"
    out[viewer_id] = "self"
    return out


async def post_payloads(
    db: AsyncSession,
    viewer: User,
    rows: Sequence[tuple[SocialPost, SocialProfile, User]],
) -> list[PostRead]:
    """Turn a page of post rows into full payloads in a FIXED number of
    queries — media, likes, comment counts, tags, mentions and cards are
    each fetched once for the whole page, never once per post."""
    if not rows:
        return []
    posts = [row[0] for row in rows]
    post_ids = [p.id for p in posts]

    media = await _media_by_post(db, post_ids)
    like_counts, liked_by_viewer = await _like_state(db, viewer.id, post_ids)
    comment_counts = await _comment_counts(db, post_ids)
    tags = await _tags_by_post(db, post_ids)
    mentions = await _mentions_by_post(db, post_ids)
    cards = await _cards_by_id(db, [p.card_id for p in posts if p.card_id])
    rels = await relationships_to(db, viewer.id, [p.author_id for p in posts])
    staff = is_admin_user(viewer)

    return [
        PostRead(
            id=post.id,
            author=author_card(profile, account, rels.get(post.author_id, "none")),
            body=post.body,
            media=media.get(post.id, []),
            card=cards.get(post.card_id) if post.card_id else None,
            created_at=post.created_at,
            like_count=like_counts.get(post.id, 0),
            comment_count=comment_counts.get(post.id, 0),
            viewer_has_liked=post.id in liked_by_viewer,
            hashtags=tags.get(post.id, []),
            mentions=mentions.get(post.id, []),
            can_delete=staff or post.author_id == viewer.id,
        )
        for post, profile, account in rows
    ]


async def _media_by_post(
    db: AsyncSession, post_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[PostMediaRead]]:
    out: dict[uuid.UUID, list[PostMediaRead]] = {}
    rows = (
        await db.execute(
            select(SocialPostMedia)
            .where(SocialPostMedia.post_id.in_(post_ids))
            .order_by(SocialPostMedia.post_id, SocialPostMedia.position)
        )
    ).scalars()
    for row in rows:
        out.setdefault(row.post_id, []).append(
            PostMediaRead(
                id=row.id,
                url=post_media.media_url(row.id),
                position=row.position,
                width=row.width,
                height=row.height,
            )
        )
    return out


async def _like_state(
    db: AsyncSession, viewer_id: uuid.UUID, post_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], set[uuid.UUID]]:
    counts = {
        pid: int(n)
        for pid, n in (
            await db.execute(
                select(SocialPostLike.post_id, func.count())
                .where(SocialPostLike.post_id.in_(post_ids))
                .group_by(SocialPostLike.post_id)
            )
        ).all()
    }
    mine = {
        row[0]
        for row in (
            await db.execute(
                select(SocialPostLike.post_id).where(
                    SocialPostLike.user_id == viewer_id,
                    SocialPostLike.post_id.in_(post_ids),
                )
            )
        ).all()
    }
    return counts, mine


async def _comment_counts(
    db: AsyncSession, post_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    return {
        pid: int(n)
        for pid, n in (
            await db.execute(
                select(SocialPostComment.post_id, func.count())
                .where(SocialPostComment.post_id.in_(post_ids))
                .group_by(SocialPostComment.post_id)
            )
        ).all()
    }


async def _tags_by_post(
    db: AsyncSession, post_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    for post_id, tag in (
        await db.execute(
            select(SocialPostHashtag.post_id, SocialPostHashtag.tag)
            .where(SocialPostHashtag.post_id.in_(post_ids))
            .order_by(SocialPostHashtag.tag)
        )
    ).all():
        out.setdefault(post_id, []).append(tag)
    return out


async def _mentions_by_post(
    db: AsyncSession, post_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    for post_id, handle in (
        await db.execute(
            select(SocialPostMention.post_id, SocialProfile.username)
            .join(SocialProfile, SocialProfile.user_id == SocialPostMention.user_id)
            .where(SocialPostMention.post_id.in_(post_ids))
        )
    ).all():
        out.setdefault(post_id, []).append(handle)
    return out


async def _cards_by_id(
    db: AsyncSession, card_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, PostCardRef]:
    if not card_ids:
        return {}
    rows = (
        await db.execute(
            select(Card, CardSet.name)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(Card.id.in_(list(dict.fromkeys(card_ids))))
        )
    ).all()
    return {
        card.id: PostCardRef(
            card_id=card.id,
            name=card.name,
            image_url=card.image_url,
            set_name=set_name,
            number=card.number,
            tcg=card.tcg.value if card.tcg else None,
        )
        for card, set_name in rows
    }


__all__ = [
    "DEFAULT_PAGE",
    "HASHTAG_RE",
    "INLINE_REPLIES",
    "MAX_PAGE",
    "MENTION_RE",
    "author_card",
    "base_post_query",
    "decode_cursor",
    "decode_offset",
    "encode_cursor",
    "encode_offset",
    "extract_hashtags",
    "extract_mention_handles",
    "post_payloads",
    "relationships_to",
    "resolve_mentions",
    "visible_post_ids",
    "visible_posts_predicate",
]
