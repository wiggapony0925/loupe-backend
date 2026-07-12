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

from app.auth.dependencies import require_user, user_rate_limit
from app.db import get_db
from app.models.user import User
from app.services.analytics import portfolio_overview_service

router = APIRouter(prefix="/analytics", tags=["analytics"])

# Per-USER cap: this endpoint walks the whole vault + per-card price
# history. 60/min never touches a real client (they poll at most ~1/min)
# but stops a scripted loop from turning one account into a CPU sink.
_overview_limit = user_rate_limit(
    limit=60, window_seconds=60, name="analytics.overview"
)


@router.get(
    "/overview",
    dependencies=[Depends(_overview_limit)],
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
