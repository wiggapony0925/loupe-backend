"""Finding collectors: search, suggestions, and the discover feed."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.card import Card
from app.models.grade import GradedCard
from app.models.user import User
from app.social.models import (
    SocialFollow,
    SocialFollowRequest,
    SocialProfile,
)
from app.social.schemas import (
    DiscoverRead,
    ExploreCard,
    ExploreRead,
    SocialUserCard,
)
from app.social.services._common import (
    MAX_PAGE_SIZE,
    collection_peeks,
    relationship_between,
    user_card,
)
from app.social.services.featured import featured_usernames, resolve_featured


def _exact_identifier(needle: str) -> tuple[str | None, uuid.UUID | None]:
    """An EXACT email or account id in the query box, if that's what it is.

    Both are matched exactly and never by prefix or substring. That is the
    whole safety property: "I know this person's address, find them" is a
    reasonable thing to support, while "show me every user whose email
    starts with j" is an address-harvesting tool. Neither value is ever
    echoed back — the result is an ordinary profile card.
    """
    email = (
        needle if "@" in needle and "." in needle.rsplit("@", maxsplit=1)[-1] else None
    )
    account_id: uuid.UUID | None = None
    try:
        account_id = uuid.UUID(needle)
    except (ValueError, AttributeError):
        account_id = None
    return email, account_id


async def search(
    db: AsyncSession, viewer: User, q: str, limit: int = 20
) -> list[SocialUserCard]:
    """Find collectors by handle, display name, EXACT email, or account id.

    Handle and name match loosely (a typeahead should); email and account id
    match exactly (see _exact_identifier).
    """
    needle = q.strip().lower()
    if len(needle) < 2:
        return []
    # A LEADING '@' IS PART OF HOW PEOPLE WRITE A HANDLE, not part of the
    # handle. The search box literally invites "@handle", and usernames are
    # stored bare — so matching the raw text meant typing the '@' the
    # placeholder asked for returned nothing at all. Stripped for handle and
    # name matching; `needle` keeps the '@' because that is what makes an
    # email address recognisable as one.
    handle = needle.lstrip("@")
    if not handle:
        return []
    like = f"%{handle}%"
    prefix = f"{handle}%"
    email, account_id = _exact_identifier(needle)
    matchers: list[ColumnElement[bool]] = [
        SocialProfile.username.like(like),
        func.lower(func.coalesce(User.display_name, "")).like(like),
    ]
    if email:
        matchers.append(func.lower(User.email) == email)
    if account_id:
        # Either identifier a support conversation might quote.
        matchers.append(User.id == account_id)
        matchers.append(SocialProfile.user_id == account_id)
    rows = (
        await db.execute(
            select(SocialProfile, User)
            .join(User, User.id == SocialProfile.user_id)
            .where(
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
                or_(*matchers),
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

    # CURATION WINS. An operator's featured list replaces the algorithmic
    # top slice; with no curation the ranking stands, so the page works out
    # of the box and an operator only intervenes when they want to.
    curated_handles = await featured_usernames()
    if not curated_handles:
        return DiscoverRead(
            featured=cards[:FEATURED_COUNT], more=cards[FEATURED_COUNT:]
        )

    curated_rows = await resolve_featured(db, curated_handles)
    # Featured collectors are shown even if the viewer already follows them
    # (the pool above excludes those) — being featured is an editorial
    # decision, not a follow suggestion. So relationships are resolved fresh.
    curated_peeks = await collection_peeks(db, [p.user_id for p, _ in curated_rows])
    featured = [
        user_card(
            p,
            u,
            await relationship_between(db, viewer.id, p.user_id),
            curated_peeks.get(p.user_id),
        )
        for p, u in curated_rows
        # Never feature the viewer to themselves — a "collectors to discover"
        # shelf containing you is just confusing.
        if p.user_id != viewer.id
    ]
    featured_ids = {c.user_id for c in featured}
    # `more` must stay disjoint from `featured` (the contract clients rely on).
    return DiscoverRead(
        featured=featured,
        more=[c for c in cards if c.user_id not in featured_ids],
    )


#: Tiles in one Explore page. Enough to fill several screens of mosaic
#: without turning the query into a table scan.
EXPLORE_LIMIT = 60
#: Cap per collector so one big vault cannot fill the whole grid.
EXPLORE_MAX_PER_OWNER = 4
#: Every Nth tile starts a HERO BAND. Six is the period of the mosaic:
#: a hero band is a 2x2 tile plus two stacked singles (3 cards filling a
#: 3-column x 2-row block), followed by a plain row of 3. Changing this
#: without changing the client's band math breaks the grid alignment.
EXPLORE_HERO_EVERY = 6


async def explore(
    db: AsyncSession, viewer: User, limit: int = EXPLORE_LIMIT
) -> ExploreRead:
    """The Community browse grid — card art from PUBLIC collections.

    A people directory answered "who is here"; this answers "what is here",
    which is the question a collector actually opens the tab with. Ranked by
    value so the grid leads with cards worth looking at.

    Only public accounts, only cards WITH art, and never the viewer's own
    holdings — a browse surface showing you your own cards is a mirror.
    At most a few tiles per collector so one large vault can't own the grid.
    """
    rows = (
        await db.execute(
            select(GradedCard, Card, SocialProfile)
            .join(Card, Card.id == GradedCard.card_id)
            .join(SocialProfile, SocialProfile.user_id == GradedCard.user_id)
            .join(User, User.id == GradedCard.user_id)
            .where(
                SocialProfile.is_private.is_(False),
                GradedCard.deleted_at.is_(None),
                GradedCard.user_id != viewer.id,
                Card.image_url.is_not(None),
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
            )
            .order_by(
                GradedCard.estimated_value_usd.desc().nulls_last(),
                GradedCard.id.desc(),
            )
            .limit(limit * 3)
        )
    ).all()

    per_owner: dict[uuid.UUID, int] = {}
    cards: list[ExploreCard] = []
    for grade, card, profile in rows:
        # A vault with a thousand cards would otherwise BE the grid.
        seen = per_owner.get(grade.user_id, 0)
        if seen >= EXPLORE_MAX_PER_OWNER:
            continue
        per_owner[grade.user_id] = seen + 1
        cards.append(
            ExploreCard(
                id=grade.id,
                card_id=grade.card_id,
                card_name=card.name,
                image_url=card.image_url or "",
                username=profile.username,
                is_hero=len(cards) % EXPLORE_HERO_EVERY == 0,
            )
        )
        if len(cards) >= limit:
            break
    return ExploreRead(cards=cards)
