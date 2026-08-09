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
from app.social.services import safety
from app.social.services._common import (
    RESERVED_USERNAMES,
    count,
    get_profile,
    profile_read,
)
from app.social.services.graph import apply_pending_requests

# ── My profile ──


async def get_me(db: AsyncSession, user: User) -> SocialMeRead:
    profile = await get_profile(db, user.id)
    if profile is None:
        return SocialMeRead(profile=None, incoming_request_count=0)
    # Enforce the public-account invariant before REPORTING the count: this
    # endpoint is what draws the inbox badge, so healing anywhere later would
    # still show a badge for requests that can never be answered.
    if not profile.is_private:
        await apply_pending_requests(db, user)
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

    # THE HANDLE, BIO AND LOCATION ARE PUBLIC TEXT. They sit on the profile
    # header, and the handle rides along on every post byline, comment and
    # follower row — so an unscreened bio reaches further than most posts do.
    # Screened together in one call rather than three.
    await safety.enforce(
        db,
        actor=user,
        surface=safety.TARGET_PROFILE,
        target_id=user.id,
        text="\n".join(
            part
            for part in (username, payload.bio or "", payload.location or "")
            if part
        ),
        excerpt=f"@{username} · {payload.bio or ''} · {payload.location or ''}",
        refusal=(
            "That profile text looks like it breaks the community rules. "
            "Keep your handle and bio about you and your collection."
        ),
    )

    if profile is None:
        profile = SocialProfile(user_id=user.id, username=username)
        db.add(profile)
    profile.username = username
    profile.bio = payload.bio
    profile.location = payload.location
    profile.is_private = payload.is_private

    await db.commit()

    # Going public honours everyone who already asked to follow. Delegated to
    # the shared invariant rather than inlined: the inline version added a
    # follow edge per request with no duplicate check, so a requester who
    # already followed would violate the composite PK and fail the whole
    # privacy change. It also must run AFTER the commit above, so the helper
    # sees the account as public.
    if was_private and not payload.is_private:
        await apply_pending_requests(db, user)

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
    """Store a new profile picture (payload already validated by the router).

    SCREENED LIKE ANY OTHER POSTED IMAGE, and arguably more important than
    one: a caption is seen by whoever opens the post, but an avatar rides
    along on every feed row, every comment and every follower list the
    account appears in — so an unscreened one is the widest-reach image in
    the product. Same policy as posts (app/social/moderation.py): refuse
    the zero-tolerance set outright, store-and-queue anything else doubtful.
    """
    profile = await get_profile(db, user.id)
    if profile is None:
        raise HTTPException(
            status_code=409, detail="Claim a username before adding a picture"
        )

    # Nothing is written when this refuses — not the object, not the version
    # bump; enforce() raises and the case IS the record of the attempt.
    await safety.enforce(
        db,
        actor=user,
        surface=safety.TARGET_PROFILE,
        target_id=profile.user_id,
        images=[(body, content_type)],
        excerpt=f"@{profile.username} profile picture",
        refusal=(
            "That picture looks like it breaks the community rules. "
            "Try a photo of you or your collection."
        ),
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
