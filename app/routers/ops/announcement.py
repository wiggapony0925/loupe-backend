"""Public announcement banner (`/v1/announcement`).

Unauthenticated so every client — guests, signed-in users, mobile — shows the
same admin-set message. Disabled/empty unless an admin turns it on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.site_config import AnnouncementRead
from app.services import site_config_service

router = APIRouter(prefix="/announcement", tags=["announcement"])


@router.get("", response_model=AnnouncementRead, summary="Global announcement")
async def get_announcement(db: AsyncSession = Depends(get_db)) -> AnnouncementRead:
    return await site_config_service.public_announcement(db)


__all__ = ["router"]
