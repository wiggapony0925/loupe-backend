"""Admin scanner-funnel analytics (`/v1/admin/scanner`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.scanner_stats import ScannerStats
from app.services.admin import scanner_stats_service

router = APIRouter(prefix="/scanner", tags=["admin-scanner"])


@router.get("", response_model=ScannerStats, summary="Scan + identify funnel metrics")
async def get_scanner_stats(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ScannerStats:
    return await scanner_stats_service.summary(db, days=days)


__all__ = ["router"]
