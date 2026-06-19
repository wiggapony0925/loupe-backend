"""Admin applicant pipeline (`/v1/admin/applications`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.portal import (
    ApplicationStatusUpdate,
    JobApplicationDetail,
    JobApplicationRead,
)
from app.services import audit_service
from app.services.portal import career_service

router = APIRouter(prefix="/applications", tags=["admin-applications"])


@router.get("", response_model=list[JobApplicationRead], summary="List applications")
async def list_applications(
    job_id: uuid.UUID | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[JobApplicationRead]:
    return await career_service.admin_list_applications(
        db, job_id=job_id, status_filter=status_filter
    )


@router.get(
    "/{application_id}",
    response_model=JobApplicationDetail,
    summary="Get an application with its full event trail",
)
async def get_application(
    application_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> JobApplicationDetail:
    return await career_service.admin_get_application(db, application_id)


@router.patch(
    "/{application_id}/status",
    response_model=JobApplicationDetail,
    summary="Advance an application and notify the applicant",
)
async def update_application_status(
    application_id: uuid.UUID,
    payload: ApplicationStatusUpdate,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JobApplicationDetail:
    result = await career_service.update_application_status(
        db, user, application_id, payload
    )
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="application.status",
        target_table="job_applications",
        target_id=application_id,
        payload={"status": payload.status.value, "notified": payload.notify},
    )
    return result


__all__ = ["router"]
