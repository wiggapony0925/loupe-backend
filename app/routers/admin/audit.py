"""Admin audit-log viewer (`/v1/admin/audit`) — read-only access to the trail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.ops import AuditFacets, AuditPage
from app.services.admin import audit_query_service

router = APIRouter(prefix="/audit", tags=["admin-ops"])


@router.get("", response_model=AuditPage, summary="Paginated, filterable audit log")
async def list_audit(
    action: str | None = Query(None),
    target_table: str | None = Query(None),
    actor: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AuditPage:
    return await audit_query_service.page(
        db,
        action=action,
        target_table=target_table,
        actor=actor,
        page=page,
        page_size=page_size,
    )


@router.get("/facets", response_model=AuditFacets, summary="Filter dropdown values")
async def audit_facets(db: AsyncSession = Depends(get_db)) -> AuditFacets:
    return await audit_query_service.facets(db)


__all__ = ["router"]
