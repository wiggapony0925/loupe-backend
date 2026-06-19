"""Admin portal metrics (`/v1/admin/metrics`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.admin import AdminMetrics
from app.services.admin import metrics_service

router = APIRouter(prefix="/metrics", tags=["admin-metrics"])


@router.get("", response_model=AdminMetrics, summary="Portal overview metrics")
async def get_metrics(db: AsyncSession = Depends(get_db)) -> AdminMetrics:
    return await metrics_service.summary(db)


__all__ = ["router"]
