"""Admin Google Cloud surface (`/v1/admin/cloud`) — read-only Cloud Run + SQL."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.ops import CloudLogEntry, CloudStatus
from app.services.admin import gcp_service

router = APIRouter(prefix="/cloud", tags=["admin-ops"])


@router.get("", response_model=CloudStatus, summary="Cloud Run + Cloud SQL status")
async def get_cloud() -> CloudStatus:
    return await gcp_service.status()


@router.get(
    "/logs",
    response_model=list[CloudLogEntry],
    summary="Recent Cloud Run log entries (newest first)",
)
async def get_logs(
    limit: int = Query(25, ge=1, le=100),
) -> list[CloudLogEntry]:
    return await gcp_service.recent_logs(limit)


__all__ = ["router"]
