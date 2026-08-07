"""Shared collections: portfolios, sets, items, friend owners."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TypedDict

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.collection import Collection, CollectionItem
from app.models.grade import GradedCard
from app.models.sealed import SealedHolding, SealedProduct
from app.models.user import User
from app.services.collection.portfolio_service import (
    _extract_price_history,
    spark_series,
)
from app.social.models import (
    SocialFollow,
    SocialProfile,
)
from app.social.schemas import (
    FriendOwnerRead,
    SocialCollectionItem,
    SocialCollectionRead,
    SocialCollectionSet,
    SocialPortfolioItemsRead,
    SocialPortfolioRead,
    SocialSealedItem,
)
from app.social.services._common import (
    MAX_PAGE_SIZE,
    can_view,
    count,
    relationship_between,
    resolve_username,
    user_card,
)


class _Spark(TypedDict):
    spark_points: list[float]
    spark_delta_pct: float | None


def _spark_for(grade: GradedCard, card: Card | None) -> _Spark:
    """The row's trend, from the card row already joined into the query.

    Kept as a dict splat so both collection builders stay single expressions.
    Uses the vault's own `spark_series`, so the same holding shows the same
    line whether you're looking at your vault or someone's profile.
    """
    points, delta = spark_series(
        _extract_price_history(card),
        current=float(grade.estimated_value_usd or 0),
    )
    return _Spark(spark_points=points, spark_delta_pct=delta)


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
            **user_card(profile, account, "following").model_dump(),
            copies=int(n or 1),
        )
        for profile, account, n in rows
    ]


# ── Follow requests (private accounts) ──
# ── The privacy-gated collection view ──


MAX_SET_TILES = 8
MAX_SEALED_TILES = 12


def _enum_value(v: object) -> str:
    return v.value if hasattr(v, "value") else str(v)


async def collection(
    db: AsyncSession, viewer: User, username: str, limit: int, offset: int
) -> SocialCollectionRead:
    profile, _ = await resolve_username(db, username, viewer)
    rel = await relationship_between(db, viewer.id, profile.user_id)
    if not can_view(profile, rel):
        raise HTTPException(status_code=403, detail="This account is private")

    owner_alive = (
        GradedCard.user_id == profile.user_id,
        GradedCard.deleted_at.is_(None),
    )
    total = await count(
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

    # Sealed shelf — boxes/ETBs are a first-class category, valued exactly
    # like /v1/grades/summary: UNOPENED holdings only, unit value x quantity.
    sealed_alive = (
        SealedHolding.user_id == profile.user_id,
        SealedHolding.deleted_at.is_(None),
        SealedHolding.opened_at.is_(None),
    )
    row_value = SealedHolding.estimated_value_usd * SealedHolding.quantity
    sealed_count, sealed_value = (
        await db.execute(
            select(
                func.coalesce(func.sum(SealedHolding.quantity), 0),
                func.sum(row_value),
            ).where(*sealed_alive)
        )
    ).one()
    sealed_rows = (
        await db.execute(
            select(SealedHolding, SealedProduct)
            .join(SealedProduct, SealedProduct.id == SealedHolding.product_id)
            .where(*sealed_alive)
            .order_by(row_value.desc().nulls_last(), SealedProduct.name.asc())
            .limit(MAX_SEALED_TILES)
        )
    ).all()
    sealed = [
        SocialSealedItem(
            product_id=product.id,
            name=product.name,
            set_name=product.set_name,
            product_type=_enum_value(product.product_type),
            tcg=_enum_value(product.tcg),
            image_url=product.image_url,
            quantity=holding.quantity,
            estimated_value_usd=(
                holding.estimated_value_usd * holding.quantity
                if holding.estimated_value_usd is not None
                else None
            ),
        )
        for holding, product in sealed_rows
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
    all_sets = [
        SocialCollectionSet(
            name=name,
            count=int(n or 0),
            estimated_value_usd=v,
            cover_image_url=covers.get(name),
        )
        for name, n, v in set_rows
    ]
    # Capped SERVER-side: a vault spanning thirty sets must not push thirty
    # tiles at the client; total_sets lets it say "+N more" without math.
    sets = all_sets[:MAX_SET_TILES]

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
            **_spark_for(grade, card),
        )
        for grade, card, card_set in rows
    ]
    total_value = None
    if value is not None or sealed_value is not None:
        total_value = (value or Decimal(0)) + (sealed_value or Decimal(0))
    return SocialCollectionRead(
        portfolios=portfolios,
        sealed=sealed,
        sealed_count=int(sealed_count or 0),
        sealed_value_usd=sealed_value,
        total_value_usd=total_value,
        sets=sets,
        total_sets=len(all_sets),
        total_cards=total,
        estimated_value_usd=value,
        items=items,
    )


async def portfolio_items(
    db: AsyncSession,
    viewer: User,
    username: str,
    collection_id: uuid.UUID,
    limit: int,
    offset: int,
) -> SocialPortfolioItemsRead:
    """One curated binder, drilled into — the cards inside it, value-first.

    Same privacy gate as the whole-collection view; a binder id that isn't
    this collector's is a 404 (no probing other people's binder ids).
    """
    profile, _ = await resolve_username(db, username, viewer)
    rel = await relationship_between(db, viewer.id, profile.user_id)
    if not can_view(profile, rel):
        raise HTTPException(status_code=403, detail="This account is private")

    coll = (
        await db.execute(
            select(Collection).where(
                Collection.id == collection_id,
                Collection.user_id == profile.user_id,
            )
        )
    ).scalar_one_or_none()
    if coll is None:
        raise HTTPException(status_code=404, detail="No such collection")

    live = (
        CollectionItem.collection_id == coll.id,
        GradedCard.deleted_at.is_(None),
    )
    total = await count(
        db,
        select(func.count())
        .select_from(CollectionItem)
        .join(GradedCard, GradedCard.id == CollectionItem.graded_card_id)
        .where(*live),
    )
    value = (
        await db.execute(
            select(func.sum(GradedCard.estimated_value_usd))
            .select_from(CollectionItem)
            .join(GradedCard, GradedCard.id == CollectionItem.graded_card_id)
            .where(*live)
        )
    ).scalar_one()

    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .select_from(CollectionItem)
            .join(GradedCard, GradedCard.id == CollectionItem.graded_card_id)
            .join(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*live)
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
            house=_enum_value(grade.house),
            condition=(
                grade.condition.value
                if grade.condition is not None and hasattr(grade.condition, "value")
                else None
            ),
            estimated_value_usd=grade.estimated_value_usd,
            graded_at=grade.graded_at,
            **_spark_for(grade, card),
        )
        for grade, card, card_set in rows
    ]
    return SocialPortfolioItemsRead(
        id=coll.id,
        name=coll.name,
        color=coll.color,
        count=total,
        estimated_value_usd=value,
        items=items,
    )
