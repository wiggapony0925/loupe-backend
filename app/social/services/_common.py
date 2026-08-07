"""Shared internals for the social services package.

Helpers every domain module leans on: payload builders, the username
resolver (including the ``@me`` self-alias), relationship lookup, and the
privacy gate. Nothing here is an endpoint-facing operation — those live in
the sibling domain modules.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import is_admin_user
from app.models.card import Card
from app.models.grade import GradedCard
from app.models.user import User
from app.social import avatars
from app.social.models import SocialFollow, SocialFollowRequest, SocialProfile
from app.social.schemas import (
    RelationshipState,
    SocialProfileRead,
    SocialUserCard,
)

# Handles that would collide with product surfaces or invite impersonation.
# "me" doubles as the self-alias every profile endpoint accepts (see
# resolve_username), so it can never be claimable.
RESERVED_USERNAMES = {
    "admin",
    "administrator",
    "api",
    "help",
    "loupe",
    "loupe_official",
    "me",
    "moderator",
    "official",
    "root",
    "settings",
    "support",
    "system",
}

MAX_PAGE_SIZE = 100

#: Spellings of "my own profile" the API resolves server-side so clients
#: never have to look up their own handle before navigating to it.
SELF_ALIASES = {"me", "@me"}


def profile_read(profile: SocialProfile) -> SocialProfileRead:
    out = SocialProfileRead.model_validate(profile)
    out.avatar_url = avatars.avatar_url(profile)
    return out


def is_pro(user: User) -> bool:
    """Raw plan, not effective entitlement — see the schema note on is_pro."""
    return (user.plan or "").lower() == "pro"


def user_card(
    profile: SocialProfile,
    user: User,
    relationship: RelationshipState,
    preview: CollectionPeek | None = None,
) -> SocialUserCard:
    peek = preview or CollectionPeek(count=0, images=[])
    # A private collection's SIZE is part of what's private — a stranger
    # shouldn't learn someone owns 4,000 cards from a directory row.
    visible = can_view(profile, relationship)
    return SocialUserCard(
        user_id=profile.user_id,
        username=profile.username,
        display_name=user.display_name,
        avatar_url=avatars.avatar_url(profile),
        location=profile.location,
        is_private=profile.is_private,
        is_pro=is_pro(user),
        is_admin=is_admin_user(user),
        relationship=relationship,
        card_count=peek.count if visible else 0,
        preview_image_urls=list(peek.images) if visible else [],
    )


@dataclass(frozen=True)
class CollectionPeek:
    """What a collector's row shows about their collection."""

    count: int
    images: list[str]


#: How many pieces of art a directory row/tile shows.
PEEK_IMAGES = 3


async def collection_peeks(
    db: AsyncSession, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, CollectionPeek]:
    """Card count + top art for MANY collectors in two queries, not 2N.

    Called for a whole page of directory rows, so it must never fan out per
    user. Art comes from each owner's most valuable cards — the collection's
    best face, which is what a collector would choose to show.
    """
    if not user_ids:
        return {}
    ids = list(dict.fromkeys(user_ids))

    counts: dict[uuid.UUID, int] = {
        uid: int(n)
        for uid, n in (
            await db.execute(
                select(GradedCard.user_id, func.count())
                .where(GradedCard.user_id.in_(ids), GradedCard.deleted_at.is_(None))
                .group_by(GradedCard.user_id)
            )
        ).all()
    }

    # Top N per owner via a window function — one query for the whole page.
    ranked = (
        select(
            GradedCard.user_id.label("uid"),
            Card.image_url.label("image_url"),
            func.row_number()
            .over(
                partition_by=GradedCard.user_id,
                order_by=(
                    GradedCard.estimated_value_usd.desc().nulls_last(),
                    GradedCard.id.desc(),
                ),
            )
            .label("rank"),
        )
        .join(Card, Card.id == GradedCard.card_id)
        .where(
            GradedCard.user_id.in_(ids),
            GradedCard.deleted_at.is_(None),
            Card.image_url.is_not(None),
        )
        .subquery()
    )
    art: dict[uuid.UUID, list[str]] = {}
    for uid, image_url in (
        await db.execute(
            select(ranked.c.uid, ranked.c.image_url).where(ranked.c.rank <= PEEK_IMAGES)
        )
    ).all():
        art.setdefault(uid, []).append(image_url)

    return {
        uid: CollectionPeek(count=counts.get(uid, 0), images=art.get(uid, []))
        for uid in ids
    }


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> SocialProfile | None:
    return (
        await db.execute(select(SocialProfile).where(SocialProfile.user_id == user_id))
    ).scalar_one_or_none()


async def resolve_username(
    db: AsyncSession, username: str, viewer: User | None = None
) -> tuple[SocialProfile, User]:
    """Profile + account row for a handle, 404 when absent/banned/deleted.

    ``@me``/``me`` resolve to the VIEWER's own profile — server-side, so no
    client ever needs to learn its own handle before linking to it.
    """
    handle = username.strip().lower()
    if viewer is not None and handle in SELF_ALIASES:
        profile = await get_profile(db, viewer.id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        return profile, viewer
    row = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialProfile.username == handle,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row[0], row[1]


async def relationship_between(
    db: AsyncSession, viewer_id: uuid.UUID, target_id: uuid.UUID
) -> RelationshipState:
    if viewer_id == target_id:
        return "self"
    followed = (
        await db.execute(
            select(SocialFollow.follower_id).where(
                SocialFollow.follower_id == viewer_id,
                SocialFollow.followee_id == target_id,
            )
        )
    ).first()
    if followed:
        return "following"
    requested = (
        await db.execute(
            select(SocialFollowRequest.id).where(
                SocialFollowRequest.requester_id == viewer_id,
                SocialFollowRequest.target_id == target_id,
            )
        )
    ).first()
    return "requested" if requested else "none"


def can_view(profile: SocialProfile, relationship: RelationshipState) -> bool:
    return (not profile.is_private) or relationship in ("self", "following")


async def count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar_one() or 0)
