"""Graded-card endpoints (the user's collection of grades)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.card import Card, CardSet
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.services.catalog import card_resolver_service
from app.services.collection import portfolio_service
from app.utils.time import utcnow

router = APIRouter(prefix="/grades", tags=["grades"])


def _to_read(
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


_SORT_OPTIONS: dict[str, Any] = {
    # key → (sql_expr_factory, tie_breaker_factory). Wrapped in factories so
    # we don't import GradedCard fields at module load before they're bound.
    "recent": (
        lambda: GradedCard.graded_at.desc(),
        lambda: GradedCard.id.desc(),
    ),
    "oldest": (
        lambda: GradedCard.graded_at.asc(),
        lambda: GradedCard.id.asc(),
    ),
    "value_desc": (
        # NULLs LAST so empty estimates don't dominate the head of the list.
        lambda: GradedCard.estimated_value_usd.desc().nulls_last(),
        lambda: GradedCard.id.desc(),
    ),
    "value_asc": (
        lambda: GradedCard.estimated_value_usd.asc().nulls_last(),
        lambda: GradedCard.id.asc(),
    ),
    "grade_desc": (
        lambda: GradedCard.grade.desc(),
        lambda: GradedCard.id.desc(),
    ),
    "grade_asc": (
        lambda: GradedCard.grade.asc(),
        lambda: GradedCard.id.asc(),
    ),
}


@router.get("", response_model=list[GradedCardRead], summary="List my graded cards")
async def list_mine(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(
        500,
        ge=1,
        le=1000,
        description=(
            "Hard cap on rows returned in a single response so large vaults "
            "don't OOM the mobile client. Defaults to 500 — more than any "
            "real collector currently owns. Combine with `cursor` to page."
        ),
    ),
    cursor: int = Query(
        0,
        ge=0,
        description=(
            "Zero-based offset into the sorted result set. Used together "
            "with `limit` for infinite-scroll pagination. The client should "
            "increment by `limit` between requests."
        ),
    ),
    q: str | None = Query(
        None,
        max_length=120,
        description=(
            "Free-text search. Case-insensitive substring match across the "
            "card name and set name. Backend search keeps mobile responsive "
            "even on 5k-card vaults where client-side filtering would stall."
        ),
    ),
    set_name: str | None = Query(
        None,
        alias="set",
        max_length=120,
        description="Filter to a single set by exact name (case-sensitive).",
    ),
    house: str | None = Query(
        None,
        max_length=16,
        description=(
            "Filter by grading house slug (e.g. `loupe`, `psa`, `bgs`). "
            "Case-insensitive — normalised to lower."
        ),
    ),
    min_grade: float | None = Query(
        None,
        ge=0,
        le=10,
        description="Minimum grade (inclusive). Rows below this are dropped.",
    ),
    sort: str = Query(
        "recent",
        description=(
            "Result ordering. One of: `recent` (default), `oldest`, "
            "`value_desc`, `value_asc`, `grade_desc`, `grade_asc`."
        ),
    ),
) -> list[GradedCardRead]:
    from sqlalchemy import func as _func, or_

    if sort not in _SORT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"sort must be one of {sorted(_SORT_OPTIONS)}",
        )

    # Base predicate — every filter below stacks on top of this so it
    # applies uniformly to both the row query and the copy-count rollup.
    base_where = [
        GradedCard.user_id == user.id,
        GradedCard.deleted_at.is_(None),
    ]
    if house is not None:
        base_where.append(GradedCard.house == house.lower())
    if min_grade is not None:
        # SQLAlchemy compares Decimal vs float fine; keep as float for clarity.
        base_where.append(GradedCard.grade >= min_grade)

    # Set & free-text filters touch joined Card/CardSet columns so they
    # have to be applied to the SELECT, not the copy-count rollup (which
    # joins only graded_cards).
    join_where = list(base_where)
    if set_name is not None:
        join_where.append(CardSet.name == set_name)
    if q:
        like = f"%{q.lower()}%"
        join_where.append(
            or_(
                _func.lower(Card.name).like(like),
                _func.lower(CardSet.name).like(like),
            )
        )

    order_factory, tie_factory = _SORT_OPTIONS[sort]

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
    # Per-card copy counts. Scope to *base* filters only — the visible
    # row may be hidden by the search/set filter while its sibling copy
    # is shown, but the "x3" badge should still reflect total ownership.
    count_rows = (
        await db.execute(
            select(GradedCard.card_id, _func.count(GradedCard.id))
            .where(*base_where)
            .group_by(GradedCard.card_id)
        )
    ).all()
    copies_by_card = {cid: int(n) for (cid, n) in count_rows}
    return [
        _to_read(g, c, s, copies_owned=copies_by_card.get(g.card_id, 1))
        for (g, c, s) in rows
    ]


@router.post(
    "",
    response_model=GradedCardRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a graded-card record",
)
async def create(
    payload: GradedCardCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
    # Resolve / materialize the card identity first so users can submit a
    # composite upstream id (e.g. "pokemontcg:base1-4") without pre-creating
    # a local Card.
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
    return GradedCardRead.model_validate(row)


# NOTE: literal-path routes MUST be declared before `/{grade_id}` so they
# aren't shadowed by the UUID-parsing path parameter.
@router.get(
    "/summary",
    summary="Portfolio aggregates for the signed-in user",
    description=(
        "Returns `{ totalValueUsd, cardCount, avgGrade, avgAccuracy, "
        "totalCostUsd, costBasisCardCount, unrealizedPnlUsd, "
        "unrealizedPnlPct }`. All values are computed from the user's real "
        "graded cards; `avgAccuracy` is null until the scan pipeline "
        "reports per-job accuracy. The cost-basis fields are null when no "
        "card has a recorded purchase price (so the UI can hide P/L "
        "rather than display a misleading `$0`)."
    ),
)
async def get_summary(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return await portfolio_service.summary(db, user)


@router.get(
    "/history",
    summary="Portfolio value over time",
    description=(
        "Returns `{ range, points: [{date, priceUsd}], deltaUsd, deltaPct }`. "
        "Computed from the per-card `price_history` populated by the daily "
        "`price_backfill` worker. Empty array when the user has no graded "
        "cards or no upstream price data has been backfilled yet."
    ),
)
async def get_history(
    range: Literal["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"] = Query("1Y"),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await portfolio_service.history(db, user, range)
    return result.to_dict()


@router.get(
    "/sparklines",
    summary="Per-card 14-point trend",
    description=(
        "Returns `[{cardId, points: number[14], deltaPct}, ...]`. Each entry "
        "is the graded-card id (not the catalog card id) so the client can "
        "map directly to vault rows. Cards with no upstream price history "
        "yield a flat line at their current estimate."
    ),
)
async def get_sparklines(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    return await portfolio_service.sparklines(db, user)


@router.get("/{grade_id}", response_model=GradedCardRead, summary="Get one graded card")
async def get_one(
    grade_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
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
    return _to_read(pair[0], pair[1], pair[2])


@router.patch(
    "/{grade_id}", response_model=GradedCardRead, summary="Update notes/value"
)
async def update(
    grade_id: uuid.UUID,
    payload: GradedCardUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
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
    if payload.grade is not None:
        row.grade = payload.grade
    if payload.house is not None:
        row.house = payload.house
    if payload.subgrades is not None:
        row.subgrades = payload.subgrades
    if payload.notes is not None:
        row.notes = payload.notes
    if payload.estimated_value_usd is not None:
        row.estimated_value_usd = payload.estimated_value_usd
    if payload.purchase_price_usd is not None:
        row.purchase_price_usd = payload.purchase_price_usd
    if payload.purchase_date is not None:
        row.purchase_date = payload.purchase_date
    await db.commit()
    await db.refresh(row)
    return GradedCardRead.model_validate(row)


@router.delete("/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete(
    grade_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
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
    row.deleted_at = utcnow()
    await db.commit()


__all__ = ["router"]
