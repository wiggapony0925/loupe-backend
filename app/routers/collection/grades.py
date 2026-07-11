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
from app.tasks import price_freshness

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
    sets: list[str] | None = Query(
        None,
        alias="set",
        description="Filter to sets by exact name (repeatable).",
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
    graded_only: bool = Query(
        False,
        description="Show only slabbed/graded cards (house ≠ loupe).",
    ),
    raw_only: bool = Query(
        False, description="Show only raw/ungraded cards (house = loupe)."
    ),
    watchlist: bool = Query(
        False, description="Show only cards on the user's watchlist."
    ),
    collection_id: uuid.UUID | None = Query(
        None,
        description=(
            "Scope to a single collection (omit for the whole vault). The "
            "active collection scopes the dashboard, analytics, and statement "
            "PDF identically."
        ),
    ),
    sort: str = Query(
        "recent",
        description=(
            "Result ordering. One of: `recent` (default), `oldest`, "
            "`value_desc`, `value_asc`, `grade_desc`, `grade_asc`, "
            "`name_asc`, `name_desc`, `number_asc`, `number_desc`."
        ),
    ),
) -> list[GradedCardRead]:
    return await graded_card_service.list_for_user(
        db,
        user,
        limit=limit,
        cursor=cursor,
        q=q,
        set_name=None,
        sets=sets,
        house=None,
        houses=house,
        min_grade=min_grade,
        max_grade=max_grade,
        min_value=min_value,
        max_value=max_value,
        tags=tags,
        graded_only=graded_only,
        raw_only=raw_only,
        watchlist=watchlist,
        collection_id=collection_id,
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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    collection_id: uuid.UUID | None = Query(None),
) -> dict[str, Any]:
    return await portfolio_service.summary(db, user, collection_id)


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
    collection_id: uuid.UUID | None = Query(None),
) -> dict[str, Any]:
    result = await portfolio_service.history(db, user, range, collection_id)
    # Background top-up of stale owned-card prices (throttled per user) so
    # tomorrow's chart has today's points — the chart read is the natural
    # trigger now that the nightly price worker is offline.
    price_freshness.kick_owned_price_refresh(user.id)
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
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    collection_id: uuid.UUID | None = Query(None),
) -> list[dict[str, Any]]:
    return await portfolio_service.sparklines(db, user, collection_id=collection_id)


@router.get(
    "/filters",
    summary="Get metadata and option labels for all filtering options",
)
async def get_filter_metadata(
    user: User = Depends(require_user),
) -> dict[str, Any]:
    return {
        "sorts": [
            {"key": "recent", "label": "Newest"},
            {"key": "oldest", "label": "Oldest"},
            {"key": "value_desc", "label": "Value ↓"},
            {"key": "value_asc", "label": "Value ↑"},
            {"key": "grade_desc", "label": "Grade ↓"},
            {"key": "grade_asc", "label": "Grade ↑"},
        ],
        "houses": [
            {"key": "loupe", "label": "Loupe"},
            {"key": "raw", "label": "Raw"},
            {"key": "psa", "label": "PSA"},
            {"key": "bgs", "label": "BGS"},
            {"key": "cgc", "label": "CGC"},
            {"key": "sgc", "label": "SGC"},
        ],
        "priceBands": [
            {"label": "Any", "min": None, "max": None},
            {"label": "< $25", "min": None, "max": 25},
            {"label": "$25–100", "min": 25, "max": 100},
            {"label": "$100–500", "min": 100, "max": 500},
            {"label": "$500+", "min": 500, "max": None},
        ],
        "minGrades": [1, 7, 8, 9, 9.5, 10],
        "maxGrades": [10, 9.5, 9, 8],
        "tcgs": [
            {"key": "all", "label": "All"},
            {"key": "pokemon", "label": "Pokémon"},
            {"key": "magic", "label": "Magic"},
            {"key": "yugioh", "label": "Yu-Gi-Oh!"},
            {"key": "onepiece", "label": "One Piece"},
            {"key": "digimon", "label": "Digimon"},
            {"key": "lorcana", "label": "Lorcana"},
            {"key": "sports", "label": "Sports"},
        ],
    }


@router.get(
    "/count",
    summary="Filtered vault row count (fast — no card payload)",
)
async def count_mine(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    q: str | None = Query(None, max_length=120),
    sets: list[str] | None = Query(None, alias="set"),
    house: list[str] | None = Query(None),
    min_grade: float | None = Query(None, ge=0, le=10),
    max_grade: float | None = Query(None, ge=0, le=10),
    min_value: Decimal | None = Query(None, ge=0),
    max_value: Decimal | None = Query(None, ge=0),
    tags: list[str] | None = Query(None),
    graded_only: bool = Query(False),
    raw_only: bool = Query(False),
    watchlist: bool = Query(False),
    collection_id: uuid.UUID | None = Query(None),
) -> dict[str, int]:
    total = await graded_card_service.count_for_user(
        db,
        user,
        q=q,
        set_name=None,
        sets=sets,
        house=None,
        houses=house,
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
    return {"count": total}


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
