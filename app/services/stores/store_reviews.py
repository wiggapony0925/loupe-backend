"""Community reviews of physical card shops.

Resy-for-card-shops: a collector rates a store 1-5 and leaves a note, and
every shop carries the community's aggregate. Reviews are keyed on the
UPSTREAM store id (``osm:node:123``) because stores aren't rows we own.

Identity comes from the social profile, so a review is always attributable
to a real handle — writing one requires a claimed username, the same gate
following does. One review per collector per store: posting again edits
yours rather than stacking duplicates.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.stores import StoreReviewRead, StoreReviewUpsert
from app.social.models import SocialProfile, StoreReview
from app.social.services import safety

MAX_REVIEWS = 50


def _to_read(
    review: StoreReview,
    profile: SocialProfile | None,
    account: User | None,
    viewer_id: uuid.UUID | None,
) -> StoreReviewRead:
    return StoreReviewRead(
        id=review.id,
        store_id=review.store_id,
        rating=review.rating,
        body=review.body,
        created_at=review.created_at,
        username=profile.username if profile else None,
        display_name=account.display_name if account else None,
        avatar_url=(
            f"/v1/social/avatar/{profile.user_id}?v={profile.avatar_version}"
            if profile and profile.avatar_key
            else None
        ),
        is_mine=viewer_id is not None and review.user_id == viewer_id,
    )


async def aggregate(db: AsyncSession, store_id: str) -> tuple[float | None, int]:
    """(average rating, count) for one store — ``(None, 0)`` when unrated."""
    avg, count = (
        await db.execute(
            select(func.avg(StoreReview.rating), func.count(StoreReview.id)).where(
                StoreReview.store_id == store_id
            )
        )
    ).one()
    return (round(float(avg), 1) if avg is not None else None, int(count or 0))


async def aggregates(
    db: AsyncSession, store_ids: list[str]
) -> dict[str, tuple[float | None, int]]:
    """Ratings for MANY stores in one query — the map list needs them all."""
    if not store_ids:
        return {}
    rows = (
        await db.execute(
            select(
                StoreReview.store_id,
                func.avg(StoreReview.rating),
                func.count(StoreReview.id),
            )
            .where(StoreReview.store_id.in_(store_ids))
            .group_by(StoreReview.store_id)
        )
    ).all()
    return {
        sid: (round(float(avg), 1) if avg is not None else None, int(n or 0))
        for sid, avg, n in rows
    }


async def list_reviews(
    db: AsyncSession, store_id: str, viewer: User | None
) -> list[StoreReviewRead]:
    """Newest first; the caller's own review is surfaced first when present."""
    rows = (
        await db.execute(
            select(StoreReview, SocialProfile, User)
            .outerjoin(SocialProfile, SocialProfile.user_id == StoreReview.user_id)
            .outerjoin(User, User.id == StoreReview.user_id)
            .where(StoreReview.store_id == store_id)
            .order_by(StoreReview.created_at.desc())
            .limit(MAX_REVIEWS)
        )
    ).all()
    viewer_id = viewer.id if viewer else None
    reviews = [_to_read(r, p, u, viewer_id) for r, p, u in rows]
    reviews.sort(key=lambda r: (not r.is_mine,))
    return reviews


async def upsert_review(
    db: AsyncSession, user: User, store_id: str, payload: StoreReviewUpsert
) -> StoreReviewRead:
    """Write (or rewrite) the caller's review of a store."""
    profile = (
        await db.execute(select(SocialProfile).where(SocialProfile.user_id == user.id))
    ).scalar_one_or_none()
    if profile is None:
        # Same gate as following: reviews are signed with a handle.
        raise HTTPException(
            status_code=409, detail="Claim a username before reviewing a store"
        )

    existing = (
        await db.execute(
            select(StoreReview).where(
                StoreReview.store_id == store_id, StoreReview.user_id == user.id
            )
        )
    ).scalar_one_or_none()

    # A shop review is public text signed with a handle — same chokepoint as
    # a post. It was the last user-authored surface anyone could read that
    # nothing screened.
    if (payload.body or "").strip():
        await safety.enforce(
            db,
            actor=user,
            surface=safety.TARGET_REVIEW,
            target_id=existing.id if existing else uuid.uuid4(),
            text=payload.body,
            excerpt=f"review of {store_id}: {payload.body}",
            refusal=(
                "That review looks like it breaks the community rules. "
                "Keep it about the shop."
            ),
        )

    if existing is None:
        existing = StoreReview(
            store_id=store_id,
            user_id=user.id,
            rating=payload.rating,
            body=(payload.body or "").strip() or None,
        )
        db.add(existing)
    else:
        existing.rating = payload.rating
        existing.body = (payload.body or "").strip() or None

    await db.commit()
    await db.refresh(existing)
    return _to_read(existing, profile, user, user.id)


async def delete_review(db: AsyncSession, user: User, store_id: str) -> None:
    """Remove the caller's review (idempotent)."""
    await db.execute(
        delete(StoreReview).where(
            StoreReview.store_id == store_id, StoreReview.user_id == user.id
        )
    )
    await db.commit()


__all__ = [
    "aggregate",
    "aggregates",
    "delete_review",
    "list_reviews",
    "upsert_review",
]
