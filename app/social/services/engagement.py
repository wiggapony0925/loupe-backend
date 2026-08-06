"""Profile viewing, visit tracking, and likes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grade import GradedCard
from app.models.user import User
from app.social import avatars
from app.social.models import (
    SocialFollow,
    SocialProfileLike,
    SocialProfileVisit,
)
from app.social.schemas import (
    ProfileLikeRead,
    SocialProfileView,
)
from app.social.services._common import (
    can_view,
    count,
    is_pro,
    relationship_between,
    resolve_username,
)


async def view_profile(
    db: AsyncSession, viewer: User, username: str
) -> SocialProfileView:
    profile, account = await resolve_username(db, username, viewer)
    rel = await relationship_between(db, viewer.id, profile.user_id)
    follower_count = await count(
        db,
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.followee_id == profile.user_id),
    )
    following_count = await count(
        db,
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.follower_id == profile.user_id),
    )
    card_count = await count(
        db,
        select(func.count())
        .select_from(GradedCard)
        .where(
            GradedCard.user_id == profile.user_id,
            GradedCard.deleted_at.is_(None),
        ),
    )
    like_count = await count(
        db,
        select(func.count())
        .select_from(SocialProfileLike)
        .where(SocialProfileLike.profile_user_id == profile.user_id),
    )
    # Recorded before the count is read, so opening a profile you've never
    # seen shows a figure that already includes you — a stat that lags one
    # page load behind reads as broken.
    await _record_visit(db, viewer.id, profile.user_id)
    view_count = await count(
        db,
        select(func.count())
        .select_from(SocialProfileVisit)
        .where(SocialProfileVisit.profile_user_id == profile.user_id),
    )
    viewer_has_liked = (
        rel != "self"
        and await count(
            db,
            select(func.count())
            .select_from(SocialProfileLike)
            .where(
                SocialProfileLike.liker_id == viewer.id,
                SocialProfileLike.profile_user_id == profile.user_id,
            ),
        )
        > 0
    )
    return SocialProfileView(
        user_id=profile.user_id,
        username=profile.username,
        display_name=account.display_name,
        avatar_url=avatars.avatar_url(profile),
        bio=profile.bio,
        location=profile.location,
        is_private=profile.is_private,
        is_pro=is_pro(account),
        joined_at=profile.created_at,
        follower_count=follower_count,
        following_count=following_count,
        card_count=card_count,
        like_count=like_count,
        view_count=view_count,
        viewer_has_liked=viewer_has_liked,
        relationship=rel,
        can_view_collection=can_view(profile, rel),
    )


async def _record_visit(
    db: AsyncSession, viewer_id: uuid.UUID, profile_user_id: uuid.UUID
) -> None:
    """Mark that ``viewer_id`` has seen this profile. Idempotent per viewer.

    Your own visits are not counted — a stat you can raise by refreshing your
    own page is not a stat. Repeat visits only bump ``last_seen_at``.
    """
    if viewer_id == profile_user_id:
        return
    existing = await db.get(SocialProfileVisit, (viewer_id, profile_user_id))
    if existing is not None:
        existing.last_seen_at = datetime.now(UTC)
    else:
        db.add(SocialProfileVisit(viewer_id=viewer_id, profile_user_id=profile_user_id))
    await db.commit()


async def like(db: AsyncSession, viewer: User, username: str) -> ProfileLikeRead:
    """Appreciate a collector's collection. Idempotent."""
    profile, _ = await resolve_username(db, username, viewer)
    if profile.user_id == viewer.id:
        raise HTTPException(status_code=400, detail="You can't like your own profile")
    existing = await db.get(SocialProfileLike, (viewer.id, profile.user_id))
    if existing is None:
        db.add(SocialProfileLike(liker_id=viewer.id, profile_user_id=profile.user_id))
        await db.commit()
    return await _like_state(db, viewer.id, profile.user_id, liked=True)


async def unlike(db: AsyncSession, viewer: User, username: str) -> ProfileLikeRead:
    """Withdraw a like. Idempotent — unliking what you never liked is a no-op."""
    profile, _ = await resolve_username(db, username, viewer)
    existing = await db.get(SocialProfileLike, (viewer.id, profile.user_id))
    if existing is not None:
        await db.delete(existing)
        await db.commit()
    return await _like_state(db, viewer.id, profile.user_id, liked=False)


async def _like_state(
    db: AsyncSession,
    viewer_id: uuid.UUID,
    profile_user_id: uuid.UUID,
    *,
    liked: bool,
) -> ProfileLikeRead:
    total = await count(
        db,
        select(func.count())
        .select_from(SocialProfileLike)
        .where(SocialProfileLike.profile_user_id == profile_user_id),
    )
    return ProfileLikeRead(liked=liked, like_count=total)
