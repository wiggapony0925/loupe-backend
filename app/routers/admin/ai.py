"""Admin · Loupe AI — the chatbot dev tool.

Read-only observability over the AI "describe it" search: live open
conversations (who's talking to the bot right now and what they're asking),
the full ask history with thumbs up/down verdicts, and per-ask drill-in
(query, game, the model's message + candidates, latency, cache hit, and the
asker's other recent asks). Admin auth is enforced by the parent router.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.ai import telemetry
from app.services.catalog import game_registry

router = APIRouter(prefix="/ai", tags=["admin-ai"])


@router.get("/search/overview", summary="Loupe AI headline stats + open conversations")
async def overview(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """24h volume/cache/latency, 7d thumbs satisfaction, and the
    conversations active in the last 30 minutes grouped by user."""
    return await telemetry.overview(db)


@router.get("/search/logs", summary="Ask history (filterable)")
async def logs(
    feedback: str | None = Query(None, pattern="^(up|down|rated)$"),
    source: str | None = Query(None, pattern="^(ai|fallback)$"),
    game: str | None = Query(
        None, pattern=game_registry.tcg_pattern(supported_only=True)
    ),
    user_id: uuid.UUID | None = Query(None, alias="userId"),
    q: str | None = Query(None, max_length=120),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200, alias="pageSize"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Every ask the chatbot has answered, newest first."""
    return await telemetry.list_logs(
        db,
        feedback=feedback,
        source=source,
        game=game,
        user_id=user_id,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.get("/search/logs/{log_id}", summary="One ask in full + its conversation")
async def log_detail(
    log_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Drill into one exchange: the query, the model's answer, the verdict,
    and the asker's other recent asks."""
    detail = await telemetry.get_log(db, log_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Ask not found")
    return detail


__all__ = ["router"]
