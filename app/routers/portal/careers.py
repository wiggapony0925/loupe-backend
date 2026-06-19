"""Public careers endpoints (`/v1/careers`).

Browse open roles, view one by slug, apply, and track an application's
status. No auth required — applicants are not necessarily Loupe users.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.platform.rate_limit import application_submit_limit, application_track_limit
from app.schemas.portal import (
    ApplicationSubmitted,
    ApplicationTrackRead,
    JobApplicationCreate,
    JobPostingRead,
)
from app.services.portal import career_service

router = APIRouter(prefix="/careers", tags=["careers"])


@router.get("/jobs", response_model=list[JobPostingRead], summary="List open roles")
async def list_jobs(db: AsyncSession = Depends(get_db)) -> list[JobPostingRead]:
    rows = await career_service.list_open(db)
    return [JobPostingRead.model_validate(r) for r in rows]


@router.get(
    "/jobs/{slug}", response_model=JobPostingRead, summary="Get an open role by slug"
)
async def get_job(slug: str, db: AsyncSession = Depends(get_db)) -> JobPostingRead:
    row = await career_service.get_open_by_slug(db, slug)
    return JobPostingRead.model_validate(row)


@router.post(
    "/jobs/{job_id}/apply",
    response_model=ApplicationSubmitted,
    status_code=201,
    summary="Apply to an open role",
    dependencies=[Depends(application_submit_limit)],
)
async def apply(
    job_id: uuid.UUID,
    payload: JobApplicationCreate,
    db: AsyncSession = Depends(get_db),
) -> ApplicationSubmitted:
    return await career_service.apply(db, job_id, payload)


@router.get(
    "/applications/{application_id}",
    response_model=ApplicationTrackRead,
    summary="Track an application (gated on the applicant's email)",
    dependencies=[Depends(application_track_limit)],
)
async def track(
    application_id: uuid.UUID,
    email: str = Query(..., description="The email used to apply."),
    db: AsyncSession = Depends(get_db),
) -> ApplicationTrackRead:
    return await career_service.track(db, application_id, email)


__all__ = ["router"]
