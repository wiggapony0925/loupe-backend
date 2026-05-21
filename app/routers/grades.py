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
from app.services import card_resolver_service, portfolio_service
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
            "real collector currently owns. Use the future cursor param to "
            "page beyond this limit."
        ),
    ),
) -> list[GradedCardRead]:
    from sqlalchemy import func as _func

    rows = (
        await db.execute(
            select(GradedCard, Card, CardSet)
            .outerjoin(Card, Card.id == GradedCard.card_id)
            .outerjoin(CardSet, CardSet.id == Card.set_id)
            .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
            # Stable tie-breaker on id so successive pages don't shuffle
            # rows that share the same `graded_at` timestamp.
            .order_by(GradedCard.graded_at.desc(), GradedCard.id.desc())
            .limit(limit)
        )
    ).all()
    # Per-card copy counts so each row knows "I'm one of N you own".
    count_rows = (
        await db.execute(
            select(GradedCard.card_id, _func.count(GradedCard.id))
            .where(GradedCard.user_id == user.id, GradedCard.deleted_at.is_(None))
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
