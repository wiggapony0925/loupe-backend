"""Profile lifecycle: me, claim/update, avatar, deactivation."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social import avatars
from app.social.models import (
    SocialFollow,
    SocialFollowRequest,
    SocialProfile,
    SocialProfileLike,
    SocialProfileVisit,
)
from app.social.schemas import (
    SocialMeRead,
    SocialProfileRead,
    SocialProfileUpsert,
)
from app.social.services._common import (
    RESERVED_USERNAMES,
    count,
    get_profile,
    profile_read,
)

# ── My profile ──


async def get_me(db: AsyncSession, user: User) -> SocialMeRead:
    profile = await get_profile(db, user.id)
    if profile is None:
        return SocialMeRead(profile=None, incoming_request_count=0)
    pending = await count(
        db,
        select(func.count())
        .select_from(SocialFollowRequest)
        .where(SocialFollowRequest.target_id == user.id),
    )
    return SocialMeRead(profile=profile_read(profile), incoming_request_count=pending)


async def upsert_me(
    db: AsyncSession, user: User, payload: SocialProfileUpsert
) -> SocialProfileRead:
    username = payload.username.strip().lower()
    if username in RESERVED_USERNAMES:
        raise HTTPException(status_code=409, detail="That username is reserved")

    taken = (
        await db.execute(
            select(SocialProfile.user_id).where(
                SocialProfile.username == username,
                SocialProfile.user_id != user.id,
            )
        )
    ).first()
    if taken:
        raise HTTPException(status_code=409, detail="That username is taken")

    profile = await get_profile(db, user.id)
    was_private = bool(profile.is_private) if profile else False
    if profile is None:
        profile = SocialProfile(user_id=user.id, username=username)
        db.add(profile)
    profile.username = username
    profile.bio = payload.bio
    profile.location = payload.location
    profile.is_private = payload.is_private

    # Going public honours everyone who already asked to follow.
    if was_private and not payload.is_private:
        pending = (
            (
                await db.execute(
                    select(SocialFollowRequest).where(
                        SocialFollowRequest.target_id == user.id
                    )
                )
            )
            .scalars()
            .all()
        )
        for req in pending:
            db.add(SocialFollow(follower_id=req.requester_id, followee_id=user.id))
            await db.delete(req)

    await db.commit()
    await db.refresh(profile)
    return profile_read(profile)


async def deactivate(db: AsyncSession, user: User) -> None:
    """Leave the community entirely.

    Deletes the profile and severs EVERY social edge in both directions —
    follows, pending requests, likes given and received, visit records —
    so the account vanishes from search, lists, and counts at once and the
    handle is immediately claimable again. The Loupe account itself (vault,
    settings, billing) is untouched; rejoining is just claiming a handle.
    """
    profile = await get_profile(db, user.id)
    if profile is None:
        raise HTTPException(status_code=404, detail="No community profile")

    uid = user.id
    await db.execute(
        delete(SocialFollow).where(
            or_(SocialFollow.follower_id == uid, SocialFollow.followee_id == uid)
        )
    )
    await db.execute(
        delete(SocialFollowRequest).where(
            or_(
                SocialFollowRequest.requester_id == uid,
                SocialFollowRequest.target_id == uid,
            )
        )
    )
    await db.execute(
        delete(SocialProfileLike).where(
            or_(
                SocialProfileLike.liker_id == uid,
                SocialProfileLike.profile_user_id == uid,
            )
        )
    )
    await db.execute(
        delete(SocialProfileVisit).where(
            or_(
                SocialProfileVisit.viewer_id == uid,
                SocialProfileVisit.profile_user_id == uid,
            )
        )
    )
    await db.delete(profile)
    await db.commit()


async def set_avatar(
    db: AsyncSession, user: User, body: bytes, content_type: str
) -> SocialProfileRead:
    """Store a new profile picture (payload already validated by the router)."""
    profile = await get_profile(db, user.id)
    if profile is None:
        raise HTTPException(
            status_code=409, detail="Claim a username before adding a picture"
        )
    await avatars.store_avatar(profile, body, content_type)
    await db.commit()
    await db.refresh(profile)
    return profile_read(profile)


async def avatar_bytes(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[bytes, str] | None:
    """Picture bytes + content type for the public serving endpoint."""
    profile = await get_profile(db, user_id)
    if profile is None or not profile.avatar_key:
        return None
    body = await avatars.load_avatar(user_id)
    if body is None:
        return None
    return body, profile.avatar_content_type or "image/jpeg"


# ── Viewing profiles ──
