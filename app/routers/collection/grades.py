"""Graded-card endpoints (the user's collection of grades).

Thin HTTP shell over :mod:`app.services.collection.graded_card_service`
for vault CRUD, and :mod:`app.services.collection.portfolio_service`
for the aggregate dashboards (summary / history / sparklines).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.grade import GradedCardCreate, GradedCardRead, GradedCardUpdate
from app.services.collection import graded_card_service, portfolio_service

router = APIRouter(prefix="/grades", tags=["grades"])


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
    house: list[str] | None = Query(
        None,
        description=(
            "Filter by grading house slug(s) (e.g. `loupe`, `psa`, `bgs`). "
            "Repeatable for multi-select (`?house=psa&house=bgs`); "
            "case-insensitive."
        ),
    ),
    min_grade: float | None = Query(
        None,
        ge=0,
        le=10,
        description="Minimum grade (inclusive). Rows below this are dropped.",
    ),
    max_grade: float | None = Query(
        None,
        ge=0,
        le=10,
        description="Maximum grade (inclusive). Rows above this are dropped.",
    ),
    min_value: Decimal | None = Query(
        None, ge=0, description="Minimum estimated value USD (inclusive)."
    ),
    max_value: Decimal | None = Query(
        None, ge=0, description="Maximum estimated value USD (inclusive)."
    ),
    tags: list[str] | None = Query(
        None,
        description=(
            "Filter to holdings tagged with ANY of these tags "
            "(repeatable, case-insensitive)."
        ),
    ),
    sort: str = Query(
        "recent",
        description=(
            "Result ordering. One of: `recent` (default), `oldest`, "
            "`value_desc`, `value_asc`, `grade_desc`, `grade_asc`."
        ),
    ),
) -> list[GradedCardRead]:
    return await graded_card_service.list_for_user(
        db,
        user,
        limit=limit,
        cursor=cursor,
        q=q,
        set_name=set_name,
        house=None,
        houses=house,
        min_grade=min_grade,
        max_grade=max_grade,
        min_value=min_value,
        max_value=max_value,
        tags=tags,
        sort=sort,
    )


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
    row = await graded_card_service.create(db, user, payload)
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
    row, card, card_set = await graded_card_service.get_one(db, user, grade_id)
    return graded_card_service.to_read(row, card, card_set)


@router.patch(
    "/{grade_id}", response_model=GradedCardRead, summary="Update notes/value"
)
async def update(
    grade_id: uuid.UUID,
    payload: GradedCardUpdate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> GradedCardRead:
    row = await graded_card_service.update(db, user, grade_id, payload)
    return GradedCardRead.model_validate(row)


@router.delete("/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete(
    grade_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await graded_card_service.soft_delete(db, user, grade_id)


__all__ = ["router"]
