"""Admin site config (`/v1/admin/config`) — the live Pro plan shape + the
global announcement banner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.site_config import (
    AnnouncementUpdate,
    PlanConfigUpdate,
    SiteConfigRead,
)
from app.services import audit_service, site_config_service

router = APIRouter(prefix="/config", tags=["admin-config"])


@router.get("", response_model=SiteConfigRead, summary="Get the site config")
async def get_config(db: AsyncSession = Depends(get_db)) -> SiteConfigRead:
    return site_config_service.to_read(await site_config_service.get(db))


@router.patch(
    "/plan",
    response_model=SiteConfigRead,
    summary="Update the Pro plan shape (limits + per-feature gating)",
)
async def update_plan(
    payload: PlanConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> SiteConfigRead:
    cfg = await site_config_service.update_plan(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="config.plan",
        target_table="site_config",
        target_id=cfg.id,
        payload=payload.model_dump(exclude_unset=True),
    )
    return site_config_service.to_read(cfg)


@router.patch(
    "/announcement",
    response_model=SiteConfigRead,
    summary="Update the global announcement banner",
)
async def update_announcement(
    payload: AnnouncementUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
) -> SiteConfigRead:
    cfg = await site_config_service.update_announcement(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="config.announcement",
        target_table="site_config",
        target_id=cfg.id,
        payload=payload.model_dump(exclude_unset=True),
    )
    return site_config_service.to_read(cfg)


__all__ = ["router"]
