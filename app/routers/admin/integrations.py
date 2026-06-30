"""Admin integrations surface (`/v1/admin/integrations`).

Read-only catalog of the external (second-party) services the app depends on —
configured status, capabilities, docs, and an optional live reachability probe.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.schemas.ops import IntegrationsReport
from app.services.admin import integration_service

router = APIRouter(prefix="/integrations", tags=["admin-ops"])


@router.get("", response_model=IntegrationsReport, summary="External-service catalog")
async def get_integrations(
    probe: bool = Query(
        default=False,
        description="Live-ping each configured service for reachability.",
    ),
) -> IntegrationsReport:
    return await integration_service.report(probe=probe)


__all__ = ["router"]
