"""Profile lifecycle: me, claim/update, avatar, deactivation."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social import avatars, moderation
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
from app.social.services.links import normalize_links
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── My profile ──


async def get_me(db: AsyncSession, user: User) -> SocialMeRead:
    profile = await get_profile(db, user.id)
    if profile is None:
        return SocialMeRead(profile=None, incoming_request_count=0)
    # Enforce the public-account invariant before REPORTING the count: this
    # endpoint is what draws the inbox badge, so healing anywhere later would
    # still show a badge for requests that can never be answered.
    # Housekeeping must never fail the READ: /social/me is the gate the whole
    # Community tab hangs off, and a hiccup here used to 500 it — which the
    # app rendered as "claim a username" to people who already had one.
    if not profile.is_private:
        try:
            await apply_pending_requests(db, user)
        except Exception:  # degrade to skipping the sweep
            logger.exception("pending-request sweep failed; serving /me anyway")
            await db.rollback()
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

    # Canonicalised BEFORE screening, so the safety gate sees the URLs as
    # they will actually be published, not the raw shorthand the client
    # typed. None means "the client didn't send the field" and leaves the
    # stored links untouched further down.
    normalized_links = (
        normalize_links(payload.links) if payload.links is not None else None
    )

    # THE HANDLE, BIO, LOCATION AND LINKS ARE PUBLIC TEXT. They sit on the
    # profile header, and the handle rides along on every post byline,
    # comment and follower row — so an unscreened bio reaches further than
    # most posts do. Links go through the same gate: an impersonation-shaped
    # handle is no less impersonation for being inside a URL.
    # Screened together in one call rather than several.
    await safety.enforce(
        db,
        actor=user,
        surface=safety.TARGET_PROFILE,
        target_id=user.id,
        text="\n".join(
            part
            for part in (
                username,
                payload.bio or "",
                payload.location or "",
                *(normalized_links or {}).values(),
            )
            if part
        ),
        excerpt=f"@{username} · {payload.bio or ''} · {payload.location or ''}",
        # Identity text, not a caption: the handle is on every byline,
        # comment and follower row the moment it is saved, so "publish it
        # and review later" would mean the queue is looked at *after* it
        # has been seen by everyone. Anything that trips is refused and the
        # author picks something else.
        policy=moderation.IDENTITY,
    )

    if profile is None:
        profile = SocialProfile(user_id=user.id, username=username)
        db.add(profile)
    profile.username = username
    profile.bio = payload.bio
    profile.location = payload.location
    profile.is_private = payload.is_private
    # Only when the field was sent: links ride a settings form the clients
    # may submit without them, and an omitted field wiping a profile's
    # links would punish every client that hasn't heard of the feature.
    # Sending {} is the explicit "clear them" (it normalises to None).
    if payload.links is not None:
        profile.links = normalized_links

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
        refusal=moderation.REFUSALS["avatar"],
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
