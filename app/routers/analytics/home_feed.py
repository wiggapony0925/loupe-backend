"""Home-feed endpoint — server-rendered rails for the Command tab.

Returns the data the mobile Home screen renders, in a single
authenticated round-trip. Replaces the previous client-side fan-out
(``useTopMovers`` parallel ``useQueries`` over ``/cards/{id}`` +
``/cards/{id}/market``) with a pure-DB roll-up.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user, user_rate_limit
from app.db import get_db
from app.models.user import User
from app.services.analytics import home_feed_service as home_service

router = APIRouter(prefix="/home", tags=["home"])

# Per-USER cap — fires on every app open; 60/min is far above real usage
# but bounds a runaway client loop.
_feed_limit = user_rate_limit(limit=60, window_seconds=60, name="home.feed")


@router.get(
    "/feed",
    dependencies=[Depends(_feed_limit)],
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
    collection_id: uuid.UUID | None = Query(
        None,
        description=(
            "Scope both rails to a single collection (omit for the whole "
            "vault) — the same active-collection seam the dashboard uses."
        ),
    ),
) -> dict[str, Any]:
    return await home_service.build_feed(
        db,
        user,
        top_movers_limit=top_movers_limit,
        recent_scans_limit=recent_scans_limit,
        collection_id=collection_id,
    )


__all__ = ["router"]
