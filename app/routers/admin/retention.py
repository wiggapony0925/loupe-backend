"""Admin cohort-retention triangle (`/v1/admin/retention`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.retention import RetentionReport
from app.services.admin import retention_service

router = APIRouter(prefix="/retention", tags=["admin-retention"])


@router.get("", response_model=RetentionReport, summary="Cohort retention triangle")
async def get_retention(db: AsyncSession = Depends(get_db)) -> RetentionReport:
    return await retention_service.cohorts(db)


__all__ = ["router"]
