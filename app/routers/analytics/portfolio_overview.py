"""Portfolio overview endpoint — `/v1/analytics/overview`.

Single authenticated request returning every aggregate the mobile
Analytics tab renders: hero stats, set indexes, movers (gainers/losers),
concentration, year distribution, grade distribution, and grader split.
Replaces the previous client-side aggregation over `/v1/collection`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.services.analytics import portfolio_overview_service

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get(
    "/overview",
    summary="Server-computed analytics rollup",
    description=(
        "Returns the data the mobile Analytics tab renders, all at once. "
        "Stats, set indexes, top movers, concentration, year and grade "
        "distributions, and grader split — computed pure-DB from the "
        "authenticated user's graded cards. No N+1, no per-card fan-out."
    ),
)
async def get_overview(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    collection_id: uuid.UUID | None = Query(None),
) -> dict[str, Any]:
    return await portfolio_overview_service.build_overview(db, user, collection_id)


__all__ = ["router"]
