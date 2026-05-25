"""Home-feed endpoint — server-rendered rails for the Command tab.

Returns the data the mobile Home screen renders, in a single
authenticated round-trip. Replaces the previous client-side fan-out
(``useTopMovers`` parallel ``useQueries`` over ``/cards/{id}`` +
``/cards/{id}/market``) with a pure-DB roll-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.services.analytics import home_feed_service as home_service

router = APIRouter(prefix="/home", tags=["home"])


@router.get(
    "/feed",
    summary="Aggregated home-screen rails",
    description=(
        "Returns ``{ topMovers, recentScans }`` for the authenticated user. "
        "Top movers are scored against the trailing 1-year price history "
        "embedded in each card's metadata; recent scans are the last N "
        "graded cards in scan-time order. Both rails are bounded by the "
        "optional ``topMovers`` and ``recentScans`` query params."
    ),
)
async def get_home_feed(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    top_movers_limit: int = Query(5, ge=1, le=20, alias="topMovers"),
    recent_scans_limit: int = Query(6, ge=1, le=20, alias="recentScans"),
) -> dict[str, Any]:
    return await home_service.build_feed(
        db,
        user,
        top_movers_limit=top_movers_limit,
        recent_scans_limit=recent_scans_limit,
    )


__all__ = ["router"]
