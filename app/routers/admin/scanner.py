"""Admin scanner-funnel analytics + scan history (`/v1/admin/scanner`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.scanner_stats import (
    ScanHistoryDetail,
    ScanHistoryPage,
    ScannerStats,
    ScannerTrend,
)
from app.services.admin import scan_history_service, scanner_stats_service

router = APIRouter(prefix="/scanner", tags=["admin-scanner"])


@router.get("", response_model=ScannerStats, summary="Scan + identify funnel metrics")
async def get_scanner_stats(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ScannerStats:
    return await scanner_stats_service.summary(db, days=days)


@router.get(
    "/trend",
    response_model=ScannerTrend,
    summary="Daily speed + accuracy trend series",
)
async def get_scanner_trend(
    days: int = Query(30, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
) -> ScannerTrend:
    return await scanner_stats_service.trend(db, days=days)


@router.get(
    "/history",
    response_model=ScanHistoryPage,
    summary="Scan history log — every scan with its photo + metadata",
)
async def get_scan_history(
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user_id: uuid.UUID | None = Query(None, description="Filter to one account."),
    source: str | None = Query(None, description='"phash" | "text" | "none" | …'),
    tcg: str | None = Query(None, description="Inferred game filter."),
    provider: str | None = Query(None, description="OCR provider filter."),
    min_confidence: float | None = Query(None, ge=0, le=1),
    matched: bool | None = Query(
        None, description="true = only scans that resolved to a card; false = misses."
    ),
    db: AsyncSession = Depends(get_db),
) -> ScanHistoryPage:
    return await scan_history_service.list_scans(
        db,
        limit=limit,
        offset=offset,
        user_id=user_id,
        source=source,
        tcg=tcg,
        provider=provider,
        min_confidence=min_confidence,
        matched=matched,
    )


@router.get(
    "/history/{scan_id}",
    response_model=ScanHistoryDetail,
    summary="One scan — full candidates + raw OCR text",
)
async def get_scan_detail(
    scan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ScanHistoryDetail:
    detail = await scan_history_service.get_scan(db, scan_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return detail


__all__ = ["router"]
