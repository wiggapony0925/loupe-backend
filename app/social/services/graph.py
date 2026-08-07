"""The follow graph: follow/unfollow, requests, follower lists."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.social.models import (
    SocialFollow,
    SocialFollowRequest,
    SocialProfile,
)
from app.social.schemas import (
    FollowRequestRead,
    FollowStateRead,
    SocialUserCard,
)
from app.social.services._common import (
    MAX_PAGE_SIZE,
    can_view,
    collection_peeks,
    get_profile,
    relationship_between,
    resolve_username,
    user_card,
)

# ── Follow graph ──


async def follow(db: AsyncSession, viewer: User, username: str) -> FollowStateRead:
    profile, _ = await resolve_username(db, username, viewer)
    if profile.user_id == viewer.id:
        raise HTTPException(status_code=400, detail="You can't follow yourself")

    # The graph only contains collectors with claimed handles — otherwise
    # follower COUNTS would include people the follower LIST (a profiles
    # join) can't show. Clients catch this 409 and open the claim sheet.
    if await get_profile(db, viewer.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Claim a username before following collectors",
        )

    rel = await relationship_between(db, viewer.id, profile.user_id)
    if rel in ("following", "requested"):
        return FollowStateRead(relationship=rel)  # idempotent

    if profile.is_private:
        db.add(SocialFollowRequest(requester_id=viewer.id, target_id=profile.user_id))
        await db.commit()
        return FollowStateRead(relationship="requested")

    db.add(SocialFollow(follower_id=viewer.id, followee_id=profile.user_id))
    await db.commit()
    return FollowStateRead(relationship="following")


async def unfollow(db: AsyncSession, viewer: User, username: str) -> FollowStateRead:
    """Remove the follow edge OR cancel a pending request — both roads to 'none'."""
    profile, _ = await resolve_username(db, username, viewer)
    await db.execute(
        delete(SocialFollow).where(
            SocialFollow.follower_id == viewer.id,
            SocialFollow.followee_id == profile.user_id,
        )
    )
    await db.execute(
        delete(SocialFollowRequest).where(
            SocialFollowRequest.requester_id == viewer.id,
            SocialFollowRequest.target_id == profile.user_id,
        )
    )
    await db.commit()
    return FollowStateRead(relationship="none")


async def remove_follower(db: AsyncSession, user: User, username: str) -> None:
    """Kick a follower off MY list (Instagram's "Remove") — deletes only the
    edge pointing at me. They aren't blocked and can follow again; on a
    private profile that means going back through a request."""
    profile, _ = await resolve_username(db, username)
    edge = (
        await db.execute(
            select(SocialFollow).where(
                SocialFollow.follower_id == profile.user_id,
                SocialFollow.followee_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if edge is None:
        raise HTTPException(status_code=404, detail="They aren't following you")
    await db.delete(edge)
    await db.commit()


async def incoming_requests(db: AsyncSession, user: User) -> list[FollowRequestRead]:
    rows = (
        await db.execute(
            select(SocialFollowRequest, SocialProfile, User)
            .join(
                SocialProfile, SocialProfile.user_id == SocialFollowRequest.requester_id
            )
            .join(User, User.id == SocialFollowRequest.requester_id)
            .where(
                SocialFollowRequest.target_id == user.id,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .order_by(SocialFollowRequest.created_at.desc())
        )
    ).all()
    return [
        FollowRequestRead(
            id=req.id,
            requester=user_card(profile, account, "none"),
            created_at=req.created_at,
        )
        for req, profile, account in rows
    ]


async def _load_my_request(
    db: AsyncSession, user: User, request_id: uuid.UUID
) -> SocialFollowRequest:
    req = (
        await db.execute(
            select(SocialFollowRequest).where(
                SocialFollowRequest.id == request_id,
                SocialFollowRequest.target_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail="Follow request not found")
    return req


async def accept_request(db: AsyncSession, user: User, request_id: uuid.UUID) -> None:
    req = await _load_my_request(db, user, request_id)
    already = (
        await db.execute(
            select(SocialFollow.follower_id).where(
                SocialFollow.follower_id == req.requester_id,
                SocialFollow.followee_id == user.id,
            )
        )
    ).first()
    if not already:
        db.add(SocialFollow(follower_id=req.requester_id, followee_id=user.id))
    await db.delete(req)
    await db.commit()


async def decline_request(db: AsyncSession, user: User, request_id: uuid.UUID) -> None:
    req = await _load_my_request(db, user, request_id)
    await db.delete(req)
    await db.commit()


# ── Lists (followers / following) ──


async def _guard_list_access(
    db: AsyncSession, viewer: User, username: str
) -> SocialProfile:
    profile, _ = await resolve_username(db, username, viewer)
    rel = await relationship_between(db, viewer.id, profile.user_id)
    if not can_view(profile, rel):
        raise HTTPException(status_code=403, detail="This account is private")
    return profile


async def followers(
    db: AsyncSession, viewer: User, username: str, limit: int, offset: int
) -> list[SocialUserCard]:
    profile = await _guard_list_access(db, viewer, username)
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(SocialFollow, SocialFollow.follower_id == SocialProfile.user_id)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialFollow.followee_id == profile.user_id,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .order_by(SocialFollow.created_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(offset)
        )
    ).all()
    peeks = await collection_peeks(db, [p.user_id for p, _ in rows])
    return [
        user_card(
            p,
            u,
            await relationship_between(db, viewer.id, p.user_id),
            peeks.get(p.user_id),
        )
        for p, u in rows
    ]


async def following(
    db: AsyncSession, viewer: User, username: str, limit: int, offset: int
) -> list[SocialUserCard]:
    profile = await _guard_list_access(db, viewer, username)
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(SocialFollow, SocialFollow.followee_id == SocialProfile.user_id)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialFollow.follower_id == profile.user_id,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .order_by(SocialFollow.created_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(offset)
        )
    ).all()
    peeks = await collection_peeks(db, [p.user_id for p, _ in rows])
    return [
        user_card(
            p,
            u,
            await relationship_between(db, viewer.id, p.user_id),
            peeks.get(p.user_id),
        )
        for p, u in rows
    ]


# ── Search ──
