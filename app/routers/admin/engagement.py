"""Admin engagement / retention analytics (`/v1/admin/engagement`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.engagement import EngagementSummary
from app.services.admin import engagement_service

router = APIRouter(prefix="/engagement", tags=["admin-engagement"])


@router.get("", response_model=EngagementSummary, summary="Engagement & retention")
async def get_engagement(db: AsyncSession = Depends(get_db)) -> EngagementSummary:
    return await engagement_service.summary(db)


__all__ = ["router"]
