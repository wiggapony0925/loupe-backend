"""Admin PriceCharting tier & fallback ops (`/v1/admin/pricecharting`).

Powers the dev-portal page that visualises which subscription tier is live and
how the app is pricing as a result. Read-only overview + two actions (re-probe
capabilities, trigger a Legendary CSV sync).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services.admin import pricecharting_admin_service as svc

router = APIRouter(prefix="/pricecharting", tags=["admin-pricecharting"])


@router.get("", summary="PriceCharting tier, capabilities & fallback chain")
async def get_overview() -> dict[str, Any]:
    """Live-detected tier, the active price strategy, the full fallback chain,
    the grade-field mapping, and local mirror status (cached probe)."""
    return await svc.get_overview()


@router.post("/probe", summary="Re-probe the live account now")
async def reprobe() -> dict[str, Any]:
    """Force a fresh capability probe — call after upgrading / downgrading the
    subscription to make the app adopt the new tier immediately."""
    return await svc.get_overview(force=True)


@router.post("/sync", summary="Sync the Legendary bulk CSV price guide")
async def sync_mirror() -> dict[str, Any]:
    """Full-refresh the local price mirror from the Legendary CSV. No-op with a
    clear reason on lower tiers / when no CSV URL is configured."""
    return await svc.resync_mirror()


__all__ = ["router"]
