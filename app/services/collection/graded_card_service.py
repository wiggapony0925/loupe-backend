"""Graded-card vault persistence.

Pure SQLAlchemy CRUD + listing for the signed-in user's collection of
graded cards. Extracted from :mod:`app.routers.collection.grades` so
the router stays a thin HTTP shell. Sibling module ``grading_service``
covers the *grading computation* (image → grade); this one covers the
*ownership records* (user owns N copies of card X).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.services.catalog import card_resolver_service
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
}


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
) -> list[GradedCardRead]:
    if sort not in SORT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"sort must be one of {sorted(SORT_OPTIONS)}",
        )

    # Base predicate — every filter below stacks on top so it applies
    # uniformly to both the row query and the copy-count rollup.
    base_where = [
        GradedCard.user_id == user.id,
        GradedCard.deleted_at.is_(None),
    ]
    if house is not None:
        base_where.append(GradedCard.house == house.lower())
    if min_grade is not None:
        base_where.append(GradedCard.grade >= min_grade)

    # Set & free-text filters touch joined Card/CardSet columns so they
    # have to be applied to the SELECT, not the copy-count rollup.
    join_where = list(base_where)
    if set_name is not None:
        join_where.append(CardSet.name == set_name)
    if q:
        like = f"%{q.lower()}%"
        join_where.append(
            or_(
                func.lower(Card.name).like(like),
                func.lower(CardSet.name).like(like),
            )
        )

    order_factory, tie_factory = SORT_OPTIONS[sort]

    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(*join_where)
            .order_by(order_factory(), tie_factory())
            .offset(cursor)
            .limit(limit)
        )
    ).all()
    # Per-card copy counts. Scope to *base* filters only — a sibling
    # copy hidden by the search/set filter should still contribute to
    # the "x3" badge for total ownership.
    count_rows = (
        await db.execute(
            select(GradedCard.card_id, func.count(GradedCard.id))
            .where(*base_where)
            .group_by(GradedCard.card_id)
        )
    ).all()
    copies_by_card = {cid: int(n) for (cid, n) in count_rows}
    return [
        to_read(g, c, s, copies_owned=copies_by_card.get(g.card_id, 1))
        for (g, c, s) in rows
    ]


async def create(db: AsyncSession, user: User, payload: GradedCardCreate) -> GradedCard:
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
    if payload.grade is not None:
        row.grade = payload.grade
    if payload.house is not None:
        row.house = payload.house
    fields = payload.model_fields_set
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
    await db.commit()
    await db.refresh(row)
    return row


async def soft_delete(db: AsyncSession, user: User, grade_id: uuid.UUID) -> None:
    row = await _load_owned(db, user, grade_id)
    row.deleted_at = utcnow()
    await db.commit()


__all__ = [
    "SORT_OPTIONS",
    "create",
    "get_one",
    "list_for_user",
    "soft_delete",
    "to_read",
    "update",
]
