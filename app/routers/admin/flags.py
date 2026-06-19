"""Admin feature-flag management (`/v1/admin/flags`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.flag import FeatureFlagCreate, FeatureFlagRead, FeatureFlagUpdate
from app.services import audit_service
from app.services.admin import flag_service

router = APIRouter(prefix="/flags", tags=["admin-flags"])


@router.get("", response_model=list[FeatureFlagRead], summary="List all feature flags")
async def list_flags(db: AsyncSession = Depends(get_db)) -> list[FeatureFlagRead]:
    rows = await flag_service.list_all(db)
    return [FeatureFlagRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=FeatureFlagRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a feature flag",
)
async def create_flag(
    payload: FeatureFlagCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> FeatureFlagRead:
    row = await flag_service.create(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="flag.create",
        target_table="feature_flags",
        target_id=row.id,
        payload={"key": row.key, "enabled": row.enabled},
    )
    return FeatureFlagRead.model_validate(row)


@router.patch(
    "/{flag_id}", response_model=FeatureFlagRead, summary="Update / toggle a flag"
)
async def update_flag(
    flag_id: uuid.UUID,
    payload: FeatureFlagUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> FeatureFlagRead:
    row = await flag_service.update(db, flag_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="flag.update",
        target_table="feature_flags",
        target_id=row.id,
        payload=payload.model_dump(exclude_unset=True),
    )
    return FeatureFlagRead.model_validate(row)


@router.delete(
    "/{flag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a feature flag",
)
async def delete_flag(
    flag_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    await flag_service.delete(db, flag_id)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="flag.delete",
        target_table="feature_flags",
        target_id=flag_id,
    )


__all__ = ["router"]
