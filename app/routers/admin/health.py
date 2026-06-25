"""Admin system-health surface (`/v1/admin/health`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.ops import HealthReport
from app.services.admin import health_service

router = APIRouter(prefix="/health", tags=["admin-ops"])


@router.get("", response_model=HealthReport, summary="System health report")
async def get_health(db: AsyncSession = Depends(get_db)) -> HealthReport:
    return await health_service.report(db)


__all__ = ["router"]
