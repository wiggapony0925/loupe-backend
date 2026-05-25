"""Price-alert endpoints (`/v1/alerts`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.price_alert import PriceAlertCreate, PriceAlertRead
from app.services.market import price_alert_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get(
    "",
    response_model=list[PriceAlertRead],
    summary="List my price alerts",
    description=(
        "Returns every price alert the signed-in user has created, "
        "most-recent first. Pass `pending=true` to filter out alerts "
        "that have already fired."
    ),
)
async def list_mine(
    pending: bool = Query(False, description="Hide already-fired alerts."),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[PriceAlertRead]:
    return await price_alert_service.list_for_user(
        db, user, include_triggered=not pending
    )


@router.post(
    "",
    response_model=PriceAlertRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a price alert",
)
async def create(
    payload: PriceAlertCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> PriceAlertRead:
    return await price_alert_service.create(db, user, payload)


@router.delete(
    "/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a price alert",
)
async def delete(
    alert_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    ok = await price_alert_service.delete(db, user, alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Price alert not found")


__all__ = ["router"]
