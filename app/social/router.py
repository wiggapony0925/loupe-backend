"""HTTP surface for the social layer (``/v1/social``).

Everything here requires a signed-in user except the avatar image itself,
which is served publicly (like any CDN asset) so ``<img>`` tags on web and
mobile need no auth plumbing — profile/collection *data* stays gated.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import rate_limit
from app.social import avatars, service
from app.social.schemas import (
    DiscoverRead,
    ExploreRead,
    FollowRequestRead,
    FollowStateRead,
    FriendOwnerRead,
    ProfileLikeRead,
    SocialCollectionRead,
    SocialMeRead,
    SocialPortfolioItemsRead,
    SocialProfileRead,
    SocialProfileUpsert,
    SocialProfileView,
    SocialUserCard,
)

router = APIRouter(prefix="/social", tags=["social"])

# Follows/search are cheap but user-triggerable in bursts; keep abuse bounded.
follow_limit = rate_limit(limit=60, window_seconds=60, name="social.follow")
search_limit = rate_limit(limit=60, window_seconds=60, name="social.search")
avatar_upload_limit = rate_limit(limit=10, window_seconds=300, name="social.avatar")


# ── Me ──


@router.get("/me", response_model=SocialMeRead, summary="My social profile")
async def get_me(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialMeRead:
    """Null ``profile`` until the user claims a username (drives onboarding)."""
    return await service.get_me(db, user)


@router.put(
    "/me",
    response_model=SocialProfileRead,
    summary="Claim or update my social profile",
)
async def put_me(
    payload: SocialProfileUpsert,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialProfileRead:
    return await service.upsert_me(db, user, payload)


@router.delete(
    "/me/followers/{username}",
    status_code=204,
    summary="Remove one of my followers",
    dependencies=[Depends(follow_limit)],
)
async def remove_my_follower(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Instagram's "Remove": they stop following me but aren't blocked —
    following again is open (or a request, if my profile is private)."""
    await service.remove_follower(db, user, username)


@router.get(
    "/cards/{card_ref}/owners",
    response_model=list[FriendOwnerRead],
    summary="Collectors I follow who own this card",
)
async def card_friend_owners(
    card_ref: str,
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[FriendOwnerRead]:
    """Card refs accept a local UUID or a composite upstream id
    (`pokemontcg:base1-4`). Only people the viewer follows appear."""
    return await service.friend_owners(db, user, card_ref, limit)


@router.delete(
    "/me",
    status_code=204,
    summary="Deactivate my community profile",
)
async def deactivate_me(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Removes the profile and severs every follow/request/like/visit edge.
    The Loupe account itself is untouched — rejoining is claiming a handle."""
    await service.deactivate(db, user)


@router.post(
    "/me/avatar",
    response_model=SocialProfileRead,
    summary="Upload my profile picture",
    dependencies=[Depends(avatar_upload_limit)],
)
async def upload_avatar(
    image: UploadFile = File(..., description="JPEG/PNG/WebP, max 5 MB."),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialProfileRead:
    content_type = (image.content_type or "").lower()
    if content_type not in avatars.ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415, detail="Profile pictures must be JPEG, PNG or WebP"
        )
    body = await image.read()
    if len(body) > avatars.MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 5 MB)")
    if not body:
        raise HTTPException(status_code=400, detail="Empty upload")
    return await service.set_avatar(db, user, body, content_type)


# ── Avatar image (public, cache-friendly) ──


@router.get(
    "/avatar/{user_id}",
    summary="Profile picture bytes",
    response_class=Response,
)
async def get_avatar(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> Response:
    found = await service.avatar_bytes(db, user_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No profile picture")
    body, content_type = found
    # URLs carry ?v=N and change on every upload, so long-cache is safe.
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ── Discovery ──


@router.get(
    "/search",
    response_model=list[SocialUserCard],
    summary="Find collectors by handle or name",
    dependencies=[Depends(search_limit)],
)
async def search_users(
    q: str = Query(..., min_length=2, max_length=60),
    limit: int = Query(20, ge=1, le=50),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[SocialUserCard]:
    return await service.search(db, user, q, limit)


@router.get(
    "/discover",
    response_model=DiscoverRead,
    summary="The Community page's people shelves, composed server-side",
    dependencies=[Depends(search_limit)],
)
async def discover_collectors(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> DiscoverRead:
    """Ranked and split by the backend — `featured` and `more` are disjoint;
    clients render them verbatim (no slicing, no dedupe, no ordering)."""
    return await service.discover(db, user)


@router.get(
    "/explore",
    response_model=ExploreRead,
    summary="The Community browse grid — card art from public collections",
    dependencies=[Depends(search_limit)],
)
async def explore_cards(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ExploreRead:
    """Ranked AND laid out server-side (including which tiles are heroes),
    so every client draws the same mosaic."""
    return await service.explore(db, user)


@router.get(
    "/suggested",
    response_model=list[SocialUserCard],
    summary="Collectors you might want to follow",
    dependencies=[Depends(search_limit)],
)
async def suggested_users(
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[SocialUserCard]:
    return await service.suggested(db, user, limit)


# ── Follow requests (private-account inbox) ──


@router.get(
    "/requests",
    response_model=list[FollowRequestRead],
    summary="My pending incoming follow requests",
)
async def list_requests(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[FollowRequestRead]:
    return await service.incoming_requests(db, user)


@router.post(
    "/requests/{request_id}/accept",
    status_code=204,
    summary="Accept a follow request",
)
async def accept_request(
    request_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.accept_request(db, user, request_id)


@router.post(
    "/requests/{request_id}/decline",
    status_code=204,
    summary="Decline a follow request",
)
async def decline_request(
    request_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await service.decline_request(db, user, request_id)


# ── Other collectors ──


@router.get(
    "/users/{username}",
    response_model=SocialProfileView,
    summary="View a collector's profile",
)
async def view_profile(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialProfileView:
    return await service.view_profile(db, user, username)


@router.post(
    "/users/{username}/follow",
    response_model=FollowStateRead,
    summary="Follow (or request to follow) a collector",
    dependencies=[Depends(follow_limit)],
)
async def follow_user(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> FollowStateRead:
    return await service.follow(db, user, username)


@router.delete(
    "/users/{username}/follow",
    response_model=FollowStateRead,
    summary="Unfollow, or cancel a pending request",
    dependencies=[Depends(follow_limit)],
)
async def unfollow_user(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> FollowStateRead:
    return await service.unfollow(db, user, username)


@router.post(
    "/users/{username}/like",
    response_model=ProfileLikeRead,
    summary="Appreciate a collector's collection",
    dependencies=[Depends(follow_limit)],
)
async def like_user(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileLikeRead:
    return await service.like(db, user, username)


@router.delete(
    "/users/{username}/like",
    response_model=ProfileLikeRead,
    summary="Withdraw a like",
    dependencies=[Depends(follow_limit)],
)
async def unlike_user(
    username: str,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileLikeRead:
    return await service.unlike(db, user, username)


@router.get(
    "/users/{username}/followers",
    response_model=list[SocialUserCard],
    summary="Who follows this collector",
)
async def list_followers(
    username: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[SocialUserCard]:
    return await service.followers(db, user, username, limit, offset)


@router.get(
    "/users/{username}/following",
    response_model=list[SocialUserCard],
    summary="Who this collector follows",
)
async def list_following(
    username: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[SocialUserCard]:
    return await service.following(db, user, username, limit, offset)


@router.get(
    "/users/{username}/collection",
    response_model=SocialCollectionRead,
    summary="A collector's vault (privacy-gated)",
)
async def view_collection(
    username: str,
    limit: int = Query(60, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialCollectionRead:
    return await service.collection(db, user, username, limit, offset)


@router.get(
    "/users/{username}/collections/{collection_id}",
    response_model=SocialPortfolioItemsRead,
    summary="One of a collector's portfolios, drilled into (privacy-gated)",
)
async def view_portfolio(
    username: str,
    collection_id: uuid.UUID,
    limit: int = Query(60, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> SocialPortfolioItemsRead:
    return await service.portfolio_items(
        db, user, username, collection_id, limit, offset
    )


__all__ = ["router"]
