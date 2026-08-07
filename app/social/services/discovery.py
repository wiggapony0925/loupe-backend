"""Finding collectors: search, suggestions, and the discover feed."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grade import GradedCard
from app.models.user import User
from app.social.models import (
    SocialFollow,
    SocialFollowRequest,
    SocialProfile,
)
from app.social.schemas import (
    DiscoverRead,
    SocialUserCard,
)
from app.social.services._common import (
    MAX_PAGE_SIZE,
    collection_peeks,
    relationship_between,
    user_card,
)


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
    peeks = await collection_peeks(db, [p.user_id for p, _ in rows])
    return [user_card(p, u, "none", peeks.get(p.user_id)) for p, u in rows]


#: Server-owned composition of the Community page.
FEATURED_COUNT = 8
DISCOVER_POOL = 40


async def discover(db: AsyncSession, viewer: User) -> DiscoverRead:
    """The Community page's people shelves, composed and RANKED server-side.

    One pool query — collectors the viewer doesn't follow (and hasn't
    requested), self excluded — ordered by follower count, then collection
    size, then recency. The top slice is ``featured``; the remainder is
    ``more``. Disjoint by construction, so clients never dedupe.
    """
    followed = select(SocialFollow.followee_id).where(
        SocialFollow.follower_id == viewer.id
    )
    requested = select(SocialFollowRequest.target_id).where(
        SocialFollowRequest.requester_id == viewer.id
    )
    follower_count = (
        select(func.count())
        .where(SocialFollow.followee_id == SocialProfile.user_id)
        .correlate(SocialProfile)
        .scalar_subquery()
    )
    card_count = (
        select(func.count())
        .where(
            GradedCard.user_id == SocialProfile.user_id,
            GradedCard.deleted_at.is_(None),
        )
        .correlate(SocialProfile)
        .scalar_subquery()
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
            .order_by(
                follower_count.desc(),
                card_count.desc(),
                SocialProfile.created_at.desc(),
            )
            .limit(DISCOVER_POOL)
        )
    ).all()
    peeks = await collection_peeks(db, [p.user_id for p, _ in rows])
    cards = [user_card(p, u, "none", peeks.get(p.user_id)) for p, u in rows]
    return DiscoverRead(featured=cards[:FEATURED_COUNT], more=cards[FEATURED_COUNT:])
