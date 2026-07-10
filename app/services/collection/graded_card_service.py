"""Graded-card vault persistence.

Pure SQLAlchemy CRUD + listing for the signed-in user's collection of
graded cards. Extracted from :mod:`app.routers.collection.grades` so
the router stays a thin HTTP shell. Sibling module ``grading_service``
covers the *grading computation* (image → grade); this one covers the
*ownership records* (user owns N copies of card X).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.card_external_ref import CardExternalRef
from app.models.enums import GradeHouseEnum, RawConditionEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.models.watchlist import WatchlistItem
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.schemas.ownership import CardHolding, CardOwnership
from app.services import entitlement_service
from app.services.catalog import card_resolver_service
from app.services.collection import collection_service
from app.utils.time import utcnow


def to_read(
    row: GradedCard,
    card: Card | None,
    card_set: CardSet | None,
    copies_owned: int = 1,
) -> GradedCardRead:
    out = GradedCardRead.model_validate(row)
    if card is not None:
        out.card_name = card.name
        out.card_image_url = card.image_url
        out.card_number = card.number
        out.card_year = card.year
        out.card_tcg = card.tcg.value if hasattr(card.tcg, "value") else str(card.tcg)
    if card_set is not None:
        out.card_set_name = card_set.name
        if out.card_year is None and card_set.release_date is not None:
            out.card_year = card_set.release_date.year
    out.copies_owned = max(1, int(copies_owned))
    return out


# Order key → (primary expr factory, tie-breaker factory). Factories
# defer attribute access so the dict can sit at module scope without
# touching SQLAlchemy descriptors before mapping is configured.
SORT_OPTIONS: dict[str, Any] = {
    "recent": (GradedCard.graded_at.desc, GradedCard.id.desc),
    "oldest": (GradedCard.graded_at.asc, GradedCard.id.asc),
    "value_desc": (
        lambda: GradedCard.estimated_value_usd.desc().nulls_last(),
        GradedCard.id.desc,
    ),
    "value_asc": (
        lambda: GradedCard.estimated_value_usd.asc().nulls_last(),
        GradedCard.id.asc,
    ),
    "grade_desc": (GradedCard.grade.desc, GradedCard.id.desc),
    "grade_asc": (GradedCard.grade.asc, GradedCard.id.asc),
    # Product name A→Z / Z→A (case-insensitive, over the joined Card).
    "name_asc": (
        lambda: func.lower(Card.name).asc().nulls_last(),
        GradedCard.id.asc,
    ),
    "name_desc": (
        lambda: func.lower(Card.name).desc().nulls_last(),
        GradedCard.id.desc,
    ),
    # Card (collector) number — lexical on the printed number string.
    "number_asc": (lambda: Card.number.asc().nulls_last(), GradedCard.id.asc),
    "number_desc": (lambda: Card.number.desc().nulls_last(), GradedCard.id.desc),
}


def _vault_filter_clauses(
    user: User,
    *,
    q: str | None,
    set_name: str | None,
    house: str | None,
    houses: list[str] | None,
    sets: list[str] | None,
    min_grade: float | None,
    max_grade: float | None,
    min_value: Decimal | None,
    max_value: Decimal | None,
    tags: list[str] | None,
    graded_only: bool,
    raw_only: bool,
    watchlist: bool,
    collection_id: uuid.UUID | None,
) -> tuple[list[Any], list[Any]]:
    """Shared WHERE stacks for list/count queries."""
    base_where: list[Any] = [
        GradedCard.user_id == user.id,
        GradedCard.deleted_at.is_(None),
    ]
    house_slugs = (
        [h.lower() for h in houses if h]
        if houses
        else ([house.lower()] if house else [])
    )
    if house_slugs:
        standard_houses = [h for h in house_slugs if h != "raw"]
        preds: list[Any] = []
        if standard_houses:
            if "loupe" in standard_houses:
                others = [h for h in standard_houses if h != "loupe"]
                if others:
                    preds.append(
                        or_(
                            GradedCard.house.in_(others),
                            and_(GradedCard.house == "loupe", GradedCard.condition.is_(None)),
                        )
                    )
                else:
                    preds.append(
                        and_(GradedCard.house == "loupe", GradedCard.condition.is_(None))
                    )
            else:
                preds.append(GradedCard.house.in_(standard_houses))

        if "raw" in house_slugs:
            preds.append(
                and_(GradedCard.house == "loupe", GradedCard.condition.is_not(None))
            )

        if len(preds) > 1:
            base_where.append(or_(*preds))
        elif len(preds) == 1:
            base_where.append(preds[0])

    if graded_only:
        base_where.append(GradedCard.house != "loupe")
    if raw_only:
        base_where.append(GradedCard.house == "loupe")
    if watchlist:
        base_where.append(
            GradedCard.card_id.in_(
                select(WatchlistItem.card_id).where(WatchlistItem.user_id == user.id)
            )
        )
    scope = collection_service.holdings_scope(collection_id, user)
    if scope is not None:
        base_where.append(scope)
    if min_grade is not None:
        base_where.append(GradedCard.grade >= min_grade)
    if max_grade is not None:
        base_where.append(GradedCard.grade <= max_grade)
    if min_value is not None:
        base_where.append(GradedCard.estimated_value_usd >= min_value)
    if max_value is not None:
        base_where.append(GradedCard.estimated_value_usd <= max_value)

    join_where = list(base_where)
    if set_name is not None:
        join_where.append(CardSet.name == set_name)
    if sets:
        join_where.append(CardSet.name.in_(sets))
    if q:
        like = f"%{q.lower()}%"
        join_where.append(
            or_(
                func.lower(Card.name).like(like),
                func.lower(CardSet.name).like(like),
            )
        )

    wanted_tags = {t.lower() for t in (tags or []) if t}
    if wanted_tags:
        # JSON array containment in SQL — avoids loading the whole vault into
        # Python when a tag filter is active.
        join_where.append(
            or_(
                *[
                    func.lower(
                        cast(func.coalesce(GradedCard.tags, "[]"), String)
                    ).contains(f'"{tag}"')
                    for tag in wanted_tags
                ]
            )
        )

    return base_where, join_where


async def list_for_user(
    db: AsyncSession,
    user: User,
    *,
    limit: int,
    cursor: int,
    q: str | None,
    set_name: str | None,
    house: str | None,
    min_grade: float | None,
    sort: str,
    houses: list[str] | None = None,
    sets: list[str] | None = None,
    max_grade: float | None = None,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
    tags: list[str] | None = None,
    graded_only: bool = False,
    raw_only: bool = False,
    watchlist: bool = False,
    collection_id: uuid.UUID | None = None,
) -> list[GradedCardRead]:
    if sort not in SORT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"sort must be one of {sorted(SORT_OPTIONS)}",
        )

    base_where, join_where = _vault_filter_clauses(
        user,
        q=q,
        set_name=set_name,
        house=house,
        houses=houses,
        sets=sets,
        min_grade=min_grade,
        max_grade=max_grade,
        min_value=min_value,
        max_value=max_value,
        tags=tags,
        graded_only=graded_only,
        raw_only=raw_only,
        watchlist=watchlist,
        collection_id=collection_id,
    )

    order_factory, tie_factory = SORT_OPTIONS[sort]
    base_query = (
        select(GradedCard, Card, CardSet)
        .outerjoin(Card, Card.id == GradedCard.card_id)
        .outerjoin(CardSet, CardSet.id == Card.set_id)
        .where(*join_where)
        .order_by(order_factory(), tie_factory())
    )

    rows = list((await db.execute(base_query.offset(cursor).limit(limit))).all())

    # Copy counts only for cards on this page — not the whole vault.
    page_card_ids = {g.card_id for (g, _c, _s) in rows}
    copies_by_card: dict[uuid.UUID, int] = {}
    if page_card_ids:
        count_rows = (
            await db.execute(
                select(GradedCard.card_id, func.count(GradedCard.id))
                .where(*base_where, GradedCard.card_id.in_(page_card_ids))
                .group_by(GradedCard.card_id)
            )
        ).all()
        copies_by_card = {cid: int(n) for (cid, n) in count_rows}

    return [
        to_read(g, c, s, copies_owned=copies_by_card.get(g.card_id, 1))
        for (g, c, s) in rows
    ]


async def count_for_user(
    db: AsyncSession,
    user: User,
    *,
    q: str | None,
    set_name: str | None,
    house: str | None,
    houses: list[str] | None = None,
    sets: list[str] | None = None,
    min_grade: float | None = None,
    max_grade: float | None = None,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
    tags: list[str] | None = None,
    graded_only: bool = False,
    raw_only: bool = False,
    watchlist: bool = False,
    collection_id: uuid.UUID | None = None,
) -> int:
    """Fast filtered row count for vault filter UI (no row payload)."""
    _, join_where = _vault_filter_clauses(
        user,
        q=q,
        set_name=set_name,
        house=house,
        houses=houses,
        sets=sets,
        min_grade=min_grade,
        max_grade=max_grade,
        min_value=min_value,
        max_value=max_value,
        tags=tags,
        graded_only=graded_only,
        raw_only=raw_only,
        watchlist=watchlist,
        collection_id=collection_id,
    )
    total = (
        await db.execute(
            select(func.count())
            .select_from(GradedCard)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*join_where)
        )
    ).scalar()
    return int(total or 0)


async def create(db: AsyncSession, user: User, payload: GradedCardCreate) -> GradedCard:
    # Free tier caps the vault; Pro (or a kill-switched-off app) is unlimited.
    # Raises 402 with a structured detail the client turns into the paywall.
    await entitlement_service.enforce_can_add_card(db, user)

    # Resolve / materialize the card identity first so users can submit
    # an upstream id without pre-creating a local Card.
    card_id = payload.card_id
    if card_id is None:
        if not payload.upstream_id:
            raise HTTPException(
                status_code=400,
                detail="Either card_id or upstream_id is required",
            )
        local = await card_resolver_service.ensure_local_card(
            db, upstream_id=payload.upstream_id
        )
        if local is None:
            raise HTTPException(
                status_code=404,
                detail=f"Could not resolve upstream_id={payload.upstream_id!r}",
            )
        await db.flush()
        card_id = local.id

    row = GradedCard(
        user_id=user.id,
        card_id=card_id,
        scan_job_id=payload.scan_job_id,
        grade=payload.grade,
        house=payload.house,
        condition=payload.condition,
        subgrades=payload.subgrades,
        estimated_value_usd=payload.estimated_value_usd,
        purchase_price_usd=payload.purchase_price_usd,
        purchase_date=payload.purchase_date,
        notes=payload.notes,
        tags=payload.tags or [],
        fingerprint_hash=payload.fingerprint_hash,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_one(
    db: AsyncSession, user: User, grade_id: uuid.UUID
) -> tuple[GradedCard, Card | None, CardSet | None]:
    pair = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(
                GradedCard.id == grade_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    return pair[0], pair[1], pair[2]


async def _load_owned(db: AsyncSession, user: User, grade_id: uuid.UUID) -> GradedCard:
    row = (
        await db.execute(
            select(GradedCard).where(
                GradedCard.id == grade_id,
                GradedCard.user_id == user.id,
                GradedCard.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Graded card not found")
    return row


async def update(
    db: AsyncSession,
    user: User,
    grade_id: uuid.UUID,
    payload: GradedCardUpdate,
) -> GradedCard:
    row = await _load_owned(db, user, grade_id)
    fields = payload.model_fields_set

    # House switch owns RAW ↔ slab normalization. The schema validator rewrites
    # grade/condition/subgrades, but those derived fields are often NOT in
    # ``model_fields_set`` (client only sent ``house``) — so we apply them
    # whenever ``house`` is present rather than only when explicitly set.
    if payload.house is not None:
        row.house = payload.house
        if payload.house == GradeHouseEnum.loupe:
            row.grade = payload.grade if payload.grade is not None else Decimal("0")
            row.condition = payload.condition or RawConditionEnum.nm
            row.subgrades = None
        else:
            if payload.grade is not None:
                row.grade = payload.grade
            row.condition = None
            if "subgrades" in fields:
                row.subgrades = payload.subgrades
    else:
        if payload.grade is not None:
            row.grade = payload.grade
        if "condition" in fields:
            row.condition = payload.condition
        if "subgrades" in fields:
            row.subgrades = payload.subgrades

    if "notes" in fields:
        row.notes = payload.notes
    if "estimated_value_usd" in fields:
        row.estimated_value_usd = payload.estimated_value_usd
    if "purchase_price_usd" in fields:
        row.purchase_price_usd = payload.purchase_price_usd
    if "purchase_date" in fields:
        row.purchase_date = payload.purchase_date
    if "tags" in fields:
        # `None` (not provided) is filtered out by `model_fields_set` only when
        # omitted; an explicit `tags: null` clears to []. The validator already
        # cleaned the list.
        row.tags = payload.tags or []
    await db.commit()
    await db.refresh(row)
    return row


async def soft_delete(db: AsyncSession, user: User, grade_id: uuid.UUID) -> None:
    row = await _load_owned(db, user, grade_id)
    row.deleted_at = utcnow()
    await db.commit()


async def _resolve_local_card_id(db: AsyncSession, card_ref: str) -> uuid.UUID | None:
    """Local Card UUID for a ref (UUID or `<source>:<external_id>`), or None.

    Pure DB lookup — no provider fetch, no materialization: a card that isn't
    local yet can't be owned, so a miss correctly yields "not owned".
    """
    try:
        as_uuid: uuid.UUID | None = uuid.UUID(card_ref)
    except (ValueError, AttributeError, TypeError):
        as_uuid = None
    if as_uuid is not None:
        return (
            await db.execute(select(Card.id).where(Card.id == as_uuid))
        ).scalar_one_or_none()
    if ":" not in card_ref:
        return None
    source, _, external = card_ref.partition(":")
    return (
        await db.execute(
            select(CardExternalRef.card_id).where(
                CardExternalRef.source == source.lower(),
                CardExternalRef.external_id == external,
            )
        )
    ).scalar_one_or_none()


async def get_card_ownership(
    db: AsyncSession, user: User, card_ref: str
) -> CardOwnership:
    """Compose the signed-in user's ownership of one card (by local UUID or
    upstream composite id): every copy + per-holding and rolled-up cost basis,
    holding value, and unrealized P/L."""
    local_id = await _resolve_local_card_id(db, card_ref)
    if local_id is None:
        return CardOwnership()

    rows = (
        (
            await db.execute(
                select(GradedCard).where(
                    GradedCard.user_id == user.id,
                    GradedCard.card_id == local_id,
                    GradedCard.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return CardOwnership()

    today = utcnow().date()
    holdings: list[CardHolding] = []
    cost_total = Decimal("0")
    value_total = Decimal("0")
    has_cost = False
    has_value = False

    for r in rows:
        pl: Decimal | None = None
        pct: float | None = None
        if r.estimated_value_usd is not None and r.purchase_price_usd is not None:
            pl = r.estimated_value_usd - r.purchase_price_usd
            if r.purchase_price_usd > 0:
                pct = float(pl / r.purchase_price_usd * 100)
        anchor = r.purchase_date or r.graded_at.date()
        days = (today - anchor).days if anchor is not None else None
        if r.purchase_price_usd is not None:
            cost_total += r.purchase_price_usd
            has_cost = True
        if r.estimated_value_usd is not None:
            value_total += r.estimated_value_usd
            has_value = True
        holdings.append(
            CardHolding(
                holding_id=r.id,
                grade=r.grade,
                house=r.house,
                is_graded=r.is_graded,
                condition=r.condition,
                subgrades=r.subgrades,
                estimated_value_usd=r.estimated_value_usd,
                purchase_price_usd=r.purchase_price_usd,
                purchase_date=r.purchase_date,
                acquired_via=r.acquired_via,
                scan_job_id=r.scan_job_id,
                fingerprint_hash=r.fingerprint_hash,
                notes=r.notes,
                graded_at=r.graded_at,
                days_held=max(days, 0) if days is not None else None,
                unrealized_pl_usd=pl,
                unrealized_pl_pct=pct,
            )
        )

    cost_basis = cost_total if has_cost else None
    holding_value = value_total if has_value else None
    total_pl: Decimal | None = None
    total_pct: float | None = None
    if cost_basis is not None and holding_value is not None:
        total_pl = holding_value - cost_basis
        if cost_basis > 0:
            total_pct = float(total_pl / cost_basis * 100)

    return CardOwnership(
        owned=True,
        copies=len(rows),
        holdings=holdings,
        cost_basis_usd=cost_basis,
        holding_value_usd=holding_value,
        unrealized_pl_usd=total_pl,
        unrealized_pl_pct=total_pct,
    )


__all__ = [
    "SORT_OPTIONS",
    "count_for_user",
    "create",
    "get_card_ownership",
    "get_one",
    "list_for_user",
    "soft_delete",
    "to_read",
    "update",
]
