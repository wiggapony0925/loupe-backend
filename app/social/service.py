"""Business logic for the social layer.

Instagram semantics throughout:

* Following a **public** profile takes effect immediately.
* Following a **private** profile creates a pending request the owner must
  accept; until then the viewer sees the profile header but not the
  collection or follower lists.
* Switching a private profile to public auto-accepts everything pending
  (those people asked to follow; on a public profile a follow is instant).
* Existing followers survive a switch to private (matching Instagram).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
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
    FollowRequestRead,
    FollowStateRead,
    FriendOwnerRead,
    ProfileLikeRead,
    RelationshipState,
    SocialCollectionItem,
    SocialCollectionRead,
    SocialCollectionSet,
    SocialMeRead,
    SocialPortfolioRead,
    SocialProfileRead,
    SocialProfileUpsert,
    SocialProfileView,
    SocialUserCard,
)

# Handles that would collide with product surfaces or invite impersonation.
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


# ── Internal helpers ──


def _profile_read(profile: SocialProfile) -> SocialProfileRead:
    out = SocialProfileRead.model_validate(profile)
    out.avatar_url = avatars.avatar_url(profile)
    return out


def _is_pro(user: User) -> bool:
    """Raw plan, not effective entitlement — see the schema note on is_pro."""
    return (user.plan or "").lower() == "pro"


def _user_card(
    profile: SocialProfile, user: User, relationship: RelationshipState
) -> SocialUserCard:
    return SocialUserCard(
        user_id=profile.user_id,
        username=profile.username,
        display_name=user.display_name,
        avatar_url=avatars.avatar_url(profile),
        location=profile.location,
        is_private=profile.is_private,
        is_pro=_is_pro(user),
        relationship=relationship,
    )


async def _get_profile(db: AsyncSession, user_id: uuid.UUID) -> SocialProfile | None:
    return (
        await db.execute(select(SocialProfile).where(SocialProfile.user_id == user_id))
    ).scalar_one_or_none()


async def _resolve_username(
    db: AsyncSession, username: str
) -> tuple[SocialProfile, User]:
    """Profile + account row for a handle, 404 when absent/banned/deleted."""
    row = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialProfile.username == username.strip().lower(),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row[0], row[1]


async def _relationship(
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


def _can_view(profile: SocialProfile, relationship: RelationshipState) -> bool:
    return (not profile.is_private) or relationship in ("self", "following")


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar_one() or 0)


# ── My profile ──


async def get_me(db: AsyncSession, user: User) -> SocialMeRead:
    profile = await _get_profile(db, user.id)
    if profile is None:
        return SocialMeRead(profile=None, incoming_request_count=0)
    pending = await _count(
        db,
        select(func.count())
        .select_from(SocialFollowRequest)
        .where(SocialFollowRequest.target_id == user.id),
    )
    return SocialMeRead(profile=_profile_read(profile), incoming_request_count=pending)


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

    profile = await _get_profile(db, user.id)
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
    return _profile_read(profile)


async def deactivate(db: AsyncSession, user: User) -> None:
    """Leave the community entirely.

    Deletes the profile and severs EVERY social edge in both directions —
    follows, pending requests, likes given and received, visit records —
    so the account vanishes from search, lists, and counts at once and the
    handle is immediately claimable again. The Loupe account itself (vault,
    settings, billing) is untouched; rejoining is just claiming a handle.
    """
    profile = await _get_profile(db, user.id)
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
    profile = await _get_profile(db, user.id)
    if profile is None:
        raise HTTPException(
            status_code=409, detail="Claim a username before adding a picture"
        )
    await avatars.store_avatar(profile, body, content_type)
    await db.commit()
    await db.refresh(profile)
    return _profile_read(profile)


async def avatar_bytes(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[bytes, str] | None:
    """Picture bytes + content type for the public serving endpoint."""
    profile = await _get_profile(db, user_id)
    if profile is None or not profile.avatar_key:
        return None
    body = await avatars.load_avatar(user_id)
    if body is None:
        return None
    return body, profile.avatar_content_type or "image/jpeg"


# ── Viewing profiles ──


async def view_profile(
    db: AsyncSession, viewer: User, username: str
) -> SocialProfileView:
    profile, account = await _resolve_username(db, username)
    rel = await _relationship(db, viewer.id, profile.user_id)
    follower_count = await _count(
        db,
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.followee_id == profile.user_id),
    )
    following_count = await _count(
        db,
        select(func.count())
        .select_from(SocialFollow)
        .where(SocialFollow.follower_id == profile.user_id),
    )
    card_count = await _count(
        db,
        select(func.count())
        .select_from(GradedCard)
        .where(
            GradedCard.user_id == profile.user_id,
            GradedCard.deleted_at.is_(None),
        ),
    )
    like_count = await _count(
        db,
        select(func.count())
        .select_from(SocialProfileLike)
        .where(SocialProfileLike.profile_user_id == profile.user_id),
    )
    # Recorded before the count is read, so opening a profile you've never
    # seen shows a figure that already includes you — a stat that lags one
    # page load behind reads as broken.
    await _record_visit(db, viewer.id, profile.user_id)
    view_count = await _count(
        db,
        select(func.count())
        .select_from(SocialProfileVisit)
        .where(SocialProfileVisit.profile_user_id == profile.user_id),
    )
    viewer_has_liked = (
        rel != "self"
        and await _count(
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
        is_pro=_is_pro(account),
        joined_at=profile.created_at,
        follower_count=follower_count,
        following_count=following_count,
        card_count=card_count,
        like_count=like_count,
        view_count=view_count,
        viewer_has_liked=viewer_has_liked,
        relationship=rel,
        can_view_collection=_can_view(profile, rel),
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
    profile, _ = await _resolve_username(db, username)
    if profile.user_id == viewer.id:
        raise HTTPException(status_code=400, detail="You can't like your own profile")
    existing = await db.get(SocialProfileLike, (viewer.id, profile.user_id))
    if existing is None:
        db.add(SocialProfileLike(liker_id=viewer.id, profile_user_id=profile.user_id))
        await db.commit()
    return await _like_state(db, viewer.id, profile.user_id, liked=True)


async def unlike(db: AsyncSession, viewer: User, username: str) -> ProfileLikeRead:
    """Withdraw a like. Idempotent — unliking what you never liked is a no-op."""
    profile, _ = await _resolve_username(db, username)
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
    count = await _count(
        db,
        select(func.count())
        .select_from(SocialProfileLike)
        .where(SocialProfileLike.profile_user_id == profile_user_id),
    )
    return ProfileLikeRead(liked=liked, like_count=count)


# ── Follow graph ──


async def follow(db: AsyncSession, viewer: User, username: str) -> FollowStateRead:
    profile, _ = await _resolve_username(db, username)
    if profile.user_id == viewer.id:
        raise HTTPException(status_code=400, detail="You can't follow yourself")

    # The graph only contains collectors with claimed handles — otherwise
    # follower COUNTS would include people the follower LIST (a profiles
    # join) can't show. Clients catch this 409 and open the claim sheet.
    if await _get_profile(db, viewer.id) is None:
        raise HTTPException(
            status_code=409,
            detail="Claim a username before following collectors",
        )

    rel = await _relationship(db, viewer.id, profile.user_id)
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
    profile, _ = await _resolve_username(db, username)
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
    profile, _ = await _resolve_username(db, username)
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


async def friend_owners(
    db: AsyncSession, viewer: User, card_ref: str, limit: int = 50
) -> list[FriendOwnerRead]:
    """Collectors the viewer FOLLOWS who own this card — the "2 of your
    friends own this card" strip on card detail.

    Following already grants collection visibility (public, or an accepted
    request on private), so showing these owners leaks nothing new. People
    the viewer doesn't follow never appear, whatever their privacy setting.
    ``card_ref`` accepts a local UUID or a composite upstream id — a card
    that isn't local yet can't be owned, so a miss is an empty list.
    """
    from app.services.collection.graded_card_service import _resolve_local_card_id

    local_id = await _resolve_local_card_id(db, card_ref)
    if local_id is None:
        return []

    copies = func.count(GradedCard.id)
    rows = (
        await db.execute(
            select(SocialProfile, User, copies)
            .join(SocialFollow, SocialFollow.followee_id == SocialProfile.user_id)
            .join(User, User.id == SocialProfile.user_id)
            .join(GradedCard, GradedCard.user_id == SocialProfile.user_id)
            .where(
                SocialFollow.follower_id == viewer.id,
                GradedCard.card_id == local_id,
                GradedCard.deleted_at.is_(None),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .group_by(SocialProfile.user_id, User.id)
            .order_by(copies.desc(), SocialProfile.username.asc())
            .limit(min(limit, MAX_PAGE_SIZE))
        )
    ).all()
    return [
        FriendOwnerRead(
            **_user_card(profile, account, "following").model_dump(),
            copies=int(n or 1),
        )
        for profile, account, n in rows
    ]


# ── Follow requests (private accounts) ──


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
            requester=_user_card(profile, account, "none"),
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
    profile, _ = await _resolve_username(db, username)
    rel = await _relationship(db, viewer.id, profile.user_id)
    if not _can_view(profile, rel):
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
    return [
        _user_card(p, u, await _relationship(db, viewer.id, p.user_id)) for p, u in rows
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
    return [
        _user_card(p, u, await _relationship(db, viewer.id, p.user_id)) for p, u in rows
    ]


# ── Search ──


async def search(
    db: AsyncSession, viewer: User, q: str, limit: int = 20
) -> list[SocialUserCard]:
    """Find collectors by handle or display name (prefix matches rank first)."""
    needle = q.strip().lower()
    if len(needle) < 2:
        return []
    like = f"%{needle}%"
    prefix = f"{needle}%"
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
                or_(
                    SocialProfile.username.like(like),
                    func.lower(func.coalesce(User.display_name, "")).like(like),
                ),
            )
            .order_by(
                # Handle prefix hits first (what a typeahead expects).
                SocialProfile.username.like(prefix).desc(),
                SocialProfile.username.asc(),
            )
            .limit(min(limit, MAX_PAGE_SIZE))
        )
    ).all()
    return [
        _user_card(p, u, await _relationship(db, viewer.id, p.user_id)) for p, u in rows
    ]


async def suggested(
    db: AsyncSession, viewer: User, limit: int = 10
) -> list[SocialUserCard]:
    """Collectors to show on an empty Community page (Collectr-style list).

    Newest claimed profiles the viewer doesn't already follow (or have a
    pending request with), self excluded. Private profiles are included —
    following one simply becomes a request.
    """
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer.id
    )
    requested = select(SocialFollowRequest.target_id).where(
        SocialFollowRequest.requester_id == viewer.id
    )
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                SocialProfile.user_id != viewer.id,
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
                SocialProfile.user_id.not_in(followed),
                SocialProfile.user_id.not_in(requested),
            )
            .order_by(SocialProfile.created_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
        )
    ).all()
    # Exclusions above guarantee the relationship is "none".
    return [_user_card(p, u, "none") for p, u in rows]


# ── The privacy-gated collection view ──


async def collection(
    db: AsyncSession, viewer: User, username: str, limit: int, offset: int
) -> SocialCollectionRead:
    profile, _ = await _resolve_username(db, username)
    rel = await _relationship(db, viewer.id, profile.user_id)
    if not _can_view(profile, rel):
        raise HTTPException(status_code=403, detail="This account is private")

    owner_alive = (
        GradedCard.user_id == profile.user_id,
        GradedCard.deleted_at.is_(None),
    )
    total = await _count(
        db, select(func.count()).select_from(GradedCard).where(*owner_alive)
    )
    # Same valuation basis as /v1/grades/summary: the grade-aware
    # estimated_value_usd (holding value), never the raw market price.
    value = (
        await db.execute(
            select(func.sum(GradedCard.estimated_value_usd)).where(*owner_alive)
        )
    ).scalar_one()

    # The collector's CURATED portfolios (binders) — what users mean by
    # "my collections". Counts/values only over living holdings.
    live_item = and_(
        GradedCard.id == CollectionItem.graded_card_id,
        GradedCard.deleted_at.is_(None),
    )
    port_rows = (
        await db.execute(
            select(
                Collection,
                func.count(GradedCard.id),
                func.sum(GradedCard.estimated_value_usd),
            )
            .outerjoin(CollectionItem, CollectionItem.collection_id == Collection.id)
            .outerjoin(GradedCard, live_item)
            .where(Collection.user_id == profile.user_id)
            .group_by(Collection.id)
            .order_by(func.sum(GradedCard.estimated_value_usd).desc().nulls_last())
        )
    ).all()
    port_covers: dict[uuid.UUID, str] = {}
    if port_rows:
        cover_q = (
            await db.execute(
                select(CollectionItem.collection_id, Card.image_url)
                .join(GradedCard, live_item)
                .join(Card, Card.id == GradedCard.card_id)
                .where(
                    CollectionItem.collection_id.in_([c.id for c, _, _ in port_rows]),
                    Card.image_url.is_not(None),
                )
                .order_by(GradedCard.estimated_value_usd.desc().nulls_last())
            )
        ).all()
        for coll_id, img in cover_q:
            if coll_id not in port_covers and img:
                port_covers[coll_id] = img
    portfolios = [
        SocialPortfolioRead(
            id=coll.id,
            name=coll.name,
            color=coll.color,
            count=int(n or 0),
            estimated_value_usd=v,
            cover_image_url=port_covers.get(coll.id),
        )
        for coll, n, v in port_rows
    ]

    # Whole-collection set breakdown (page-independent): "5 Evolving Skies".
    set_name = func.coalesce(CardSet.name, "Other")
    set_rows = (
        await db.execute(
            select(
                set_name,
                func.count(GradedCard.id),
                func.sum(GradedCard.estimated_value_usd),
            )
            .select_from(GradedCard)
            .join(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*owner_alive)
            .group_by(set_name)
            .order_by(func.sum(GradedCard.estimated_value_usd).desc().nulls_last())
        )
    ).all()
    # Cover art = each set's most valuable card, resolved in one ordered scan.
    cover_rows = (
        await db.execute(
            select(set_name, Card.image_url)
            .select_from(GradedCard)
            .join(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*owner_alive, Card.image_url.is_not(None))
            .order_by(GradedCard.estimated_value_usd.desc().nulls_last())
        )
    ).all()
    covers: dict[str, str] = {}
    for name, img in cover_rows:
        if name not in covers and img:
            covers[name] = img
    sets = [
        SocialCollectionSet(
            name=name,
            count=int(n or 0),
            estimated_value_usd=v,
            cover_image_url=covers.get(name),
        )
        for name, n, v in set_rows
    ]

    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .join(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*owner_alive)
            .order_by(
                GradedCard.estimated_value_usd.desc().nulls_last(),
                GradedCard.id.desc(),
            )
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(offset)
        )
    ).all()

    items = [
        SocialCollectionItem(
            id=grade.id,
            card_id=grade.card_id,
            card_name=card.name if card else None,
            card_image_url=card.image_url if card else None,
            card_set_name=card_set.name if card_set else None,
            card_number=card.number if card else None,
            card_tcg=(card.tcg.value if card and hasattr(card.tcg, "value") else None),
            grade=grade.grade,
            house=grade.house.value
            if hasattr(grade.house, "value")
            else str(grade.house),
            condition=(
                grade.condition.value
                if grade.condition is not None and hasattr(grade.condition, "value")
                else None
            ),
            estimated_value_usd=grade.estimated_value_usd,
            graded_at=grade.graded_at,
        )
        for grade, card, card_set in rows
    ]
    return SocialCollectionRead(
        portfolios=portfolios,
        sets=sets,
        total_cards=total,
        estimated_value_usd=value,
        items=items,
    )


__all__ = [
    "accept_request",
    "collection",
    "decline_request",
    "follow",
    "followers",
    "following",
    "get_me",
    "incoming_requests",
    "search",
    "unfollow",
    "upsert_me",
    "view_profile",
]
