"""Admin Loupe Scanner waitlist pipeline (`/v1/admin/waitlist`).

List every signup, advance a signup's stage (waiting → invited →
purchased), and remove spam. Admin auth is enforced once for the whole
``/v1/admin`` subtree; handlers that audit-log re-declare ``require_admin``
to get the acting user.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.waitlist import WaitlistEntryRead, WaitlistStatusUpdate
from app.services import audit_service
from app.services.portal import waitlist_service

router = APIRouter(prefix="/waitlist", tags=["admin-waitlist"])


@router.get(
    "",
    response_model=list[WaitlistEntryRead],
    summary="List scanner waitlist signups",
)
async def list_waitlist(
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[WaitlistEntryRead]:
    return await waitlist_service.admin_list(db, status_filter=status_filter)


@router.patch(
    "/{entry_id}/status",
    response_model=WaitlistEntryRead,
    summary="Advance a waitlist signup's stage",
)
async def update_status(
    entry_id: uuid.UUID,
    payload: WaitlistStatusUpdate,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> WaitlistEntryRead:
    result = await waitlist_service.admin_update_status(db, entry_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="waitlist.status",
        target_table="waitlist_entries",
        target_id=entry_id,
        payload={"status": payload.status.value},
    )
    return result


@router.delete(
    "/{entry_id}",
    status_code=204,
    summary="Remove a waitlist signup",
)
async def delete_entry(
    entry_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    await waitlist_service.admin_delete(db, entry_id)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="waitlist.delete",
        target_table="waitlist_entries",
        target_id=entry_id,
    )


__all__ = ["router"]
