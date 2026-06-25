"""Admin revenue analytics (`/v1/admin/revenue`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.revenue import RevenueSummary
from app.services.admin import revenue_service

router = APIRouter(prefix="/revenue", tags=["admin-revenue"])


@router.get("", response_model=RevenueSummary, summary="Subscription revenue summary")
async def get_revenue(db: AsyncSession = Depends(get_db)) -> RevenueSummary:
    return await revenue_service.summary(db)


__all__ = ["router"]
