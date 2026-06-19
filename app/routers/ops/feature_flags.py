"""Public feature-flag read (`/v1/flags`).

Returns `{key: enabled}` for every flag. Public + unauthenticated so guests,
signed-in users, and the mobile app can all gate UI on the same source.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.admin import flag_service

router = APIRouter(prefix="/flags", tags=["flags"])


@router.get("", summary="Public feature-flag map")
async def get_flags(db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    return await flag_service.public_map(db)


__all__ = ["router"]
