"""Admin live activity feed (`/v1/admin/pulse`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.pulse import PulseFeed
from app.services.admin import pulse_service

router = APIRouter(prefix="/pulse", tags=["admin-pulse"])


@router.get("", response_model=PulseFeed, summary="Recent activity across the app")
async def get_pulse(
    limit: int = Query(40, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> PulseFeed:
    return await pulse_service.recent(db, limit=limit)


__all__ = ["router"]
