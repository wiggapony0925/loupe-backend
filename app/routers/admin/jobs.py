"""Admin job-posting management (`/v1/admin/jobs`)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.portal import JobPostingCreate, JobPostingRead, JobPostingUpdate
from app.services import audit_service
from app.services.portal import career_service

router = APIRouter(prefix="/jobs", tags=["admin-jobs"])


@router.get("", response_model=list[JobPostingRead], summary="List all job postings")
async def list_jobs(db: AsyncSession = Depends(get_db)) -> list[JobPostingRead]:
    rows = await career_service.admin_list_jobs(db)
    return [JobPostingRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=JobPostingRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a job posting",
)
async def create_job(
    payload: JobPostingCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> JobPostingRead:
    row = await career_service.create_job(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="job.create",
        target_table="job_postings",
        target_id=row.id,
        payload={"title": row.title, "status": row.status},
    )
    return JobPostingRead.model_validate(row)


@router.get("/{job_id}", response_model=JobPostingRead, summary="Get a job posting")
async def get_job(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> JobPostingRead:
    row = await career_service.admin_get_job(db, job_id)
    return JobPostingRead.model_validate(row)


@router.patch(
    "/{job_id}", response_model=JobPostingRead, summary="Update a job posting"
)
async def update_job(
    job_id: uuid.UUID,
    payload: JobPostingUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> JobPostingRead:
    row = await career_service.update_job(db, job_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="job.update",
        target_table="job_postings",
        target_id=row.id,
        payload=payload.model_dump(exclude_unset=True, mode="json"),
    )
    return JobPostingRead.model_validate(row)


@router.delete(
    "/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a job posting",
)
async def delete_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    await career_service.delete_job(db, job_id)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="job.delete",
        target_table="job_postings",
        target_id=job_id,
    )


__all__ = ["router"]
