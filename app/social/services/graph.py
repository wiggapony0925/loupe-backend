"""The follow graph: follow/unfollow, requests, follower lists."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
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
from app.social.services import feed_notify
from app.social.services._common import (
    MAX_PAGE_SIZE,
    can_view,
    collection_peeks,
    get_profile,
    relationship_between,
    resolve_username,
    user_card,
)
from app.utils.logger import get_logger

# ── Follow graph ──


logger = get_logger("social.graph")


async def apply_pending_requests(db: AsyncSession, user: User) -> int:
    """Resolve every pending request to a PUBLIC account. Returns how many.

    THE RULE: a pending follow request to a public account is not a valid
    state. Nobody should have to approve an ask to see something already
    visible to anyone, and an inbox badge for a decision that cannot matter
    is a lie the user can't clear.

    This is written as an INVARIANT, not a one-time fix-up, because that
    distinction is the actual bug it fixes: the private→public transition
    already converted requests, but anything that reached "public" by some
    other route — an older build that predated that code, a restore, a seed,
    a direct DB edit — left requests stuck forever, visible and unresolvable.
    So it is enforced wherever the state is READ as well as where it changes,
    and it is idempotent and cheap enough to call on both.

    Edge cases, all deliberately handled rather than left to a constraint:
      • already follows      → delete the request, add nothing (the follow
                               edge's composite PK would otherwise raise and
                               take the whole request down with it);
      • deleted/banned user  → drop the request, create no follow;
      • deactivated profile  → drop it too. The graph only holds collectors
                               with claimed handles, so a follow without a
                               profile would inflate the follower COUNT past
                               what the follower LIST (a profiles join) can
                               show;
      • self-request         → dropped (a CHECK forbids the follow edge);
      • concurrent callers   → the insert is guarded, and a loser simply
                               finds the row already there next time.
    """
    profile = await get_profile(db, user.id)
    # Private accounts are exactly where pending requests belong.
    if profile is None or profile.is_private:
        return 0

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
    if not pending:
        return 0

    requester_ids = {r.requester_id for r in pending}
    # Who is still a real, claimed, live collector.
    eligible = set(
        (
            await db.execute(
                select(SocialProfile.user_id)
                .join(User, User.id == SocialProfile.user_id)
                .where(
                    SocialProfile.user_id.in_(requester_ids),
                    User.deleted_at.is_(None),
                    User.banned_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # Who already follows — converting them again would violate the PK.
    existing = set(
        (
            await db.execute(
                select(SocialFollow.follower_id).where(
                    SocialFollow.followee_id == user.id,
                    SocialFollow.follower_id.in_(requester_ids),
                )
            )
        )
        .scalars()
        .all()
    )

    applied = 0
    added: set[uuid.UUID] = set()
    for req in pending:
        rid = req.requester_id
        # `added` also collapses duplicate pending rows from one requester.
        if rid in eligible and rid != user.id and rid not in existing | added:
            db.add(SocialFollow(follower_id=rid, followee_id=user.id))
            added.add(rid)
            applied += 1

    # Every pending row goes, applied or not: none of them can ever be
    # answered while the account is public. A bulk DELETE (not per-row ORM
    # deletes) carries no expected row count, so a concurrent caller having
    # already swept them is a no-op instead of a StaleDataError.
    await db.execute(
        delete(SocialFollowRequest).where(SocialFollowRequest.target_id == user.id)
    )

    try:
        await db.commit()
    except IntegrityError:
        # The docstring's "concurrent callers" promise, actually kept: two
        # overlapping requests both read `existing` before either committed,
        # so the loser's INSERT hits the composite PK. The winner already
        # did the work — roll back and report nothing applied.
        await db.rollback()
        return 0
    if applied:
        logger.info("auto-accepted %s follow request(s) for a public account", applied)
    return applied


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
    # After the commit: the follow is the fact, the notification is delivery.
    await feed_notify.followed(db, actor=viewer, target_id=profile.user_id)
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
    """Pending asks for MY approval.

    Heals first: a public account has no valid pending requests, and showing
    an inbox the user cannot act on is worse than showing an empty one.
    """
    await apply_pending_requests(db, user)
    return await _incoming_requests(db, user)


async def _incoming_requests(db: AsyncSession, user: User) -> list[FollowRequestRead]:
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
