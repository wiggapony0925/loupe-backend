"""Admin developer-portal endpoints (`/v1/admin`).

Job + blog management and the applicant pipeline. Every route requires an
admin user (the ``ADMIN_EMAILS`` allowlist via :func:`require_admin`).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.db import get_db
from app.models.user import User
from app.schemas.portal import (
    ApplicationStatusUpdate,
    BlogPostCreate,
    BlogPostRead,
    BlogPostUpdate,
    JobApplicationDetail,
    JobApplicationRead,
    JobPostingCreate,
    JobPostingRead,
    JobPostingUpdate,
)
from app.services import audit_service
from app.services.portal import blog_service, career_service

router = APIRouter(
    prefix="/admin", tags=["admin-portal"], dependencies=[Depends(require_admin)]
)


# ── Jobs ────────────────────────────────────────────────────────────────


@router.get(
    "/jobs", response_model=list[JobPostingRead], summary="List all job postings"
)
async def list_jobs(db: AsyncSession = Depends(get_db)) -> list[JobPostingRead]:
    rows = await career_service.admin_list_jobs(db)
    return [JobPostingRead.model_validate(r) for r in rows]


@router.post(
    "/jobs",
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


@router.get(
    "/jobs/{job_id}", response_model=JobPostingRead, summary="Get a job posting"
)
async def get_job(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> JobPostingRead:
    row = await career_service.admin_get_job(db, job_id)
    return JobPostingRead.model_validate(row)


@router.patch(
    "/jobs/{job_id}", response_model=JobPostingRead, summary="Update a job posting"
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
    "/jobs/{job_id}",
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


# ── Applications ────────────────────────────────────────────────────────


@router.get(
    "/applications",
    response_model=list[JobApplicationRead],
    summary="List applications",
)
async def list_applications(
    job_id: uuid.UUID | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[JobApplicationRead]:
    return await career_service.admin_list_applications(
        db, job_id=job_id, status_filter=status_filter
    )


@router.get(
    "/applications/{application_id}",
    response_model=JobApplicationDetail,
    summary="Get an application with its full event trail",
)
async def get_application(
    application_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> JobApplicationDetail:
    return await career_service.admin_get_application(db, application_id)


@router.patch(
    "/applications/{application_id}/status",
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


# ── Blog ────────────────────────────────────────────────────────────────


@router.get("/blog", response_model=list[BlogPostRead], summary="List all blog posts")
async def list_posts(db: AsyncSession = Depends(get_db)) -> list[BlogPostRead]:
    rows = await blog_service.admin_list(db)
    return [BlogPostRead.model_validate(r) for r in rows]


@router.post(
    "/blog",
    response_model=BlogPostRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a blog post",
)
async def create_post(
    payload: BlogPostCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BlogPostRead:
    row = await blog_service.create(db, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.create",
        target_table="blog_posts",
        target_id=row.id,
        payload={"title": row.title, "status": row.status},
    )
    return BlogPostRead.model_validate(row)


@router.get("/blog/{post_id}", response_model=BlogPostRead, summary="Get a blog post")
async def get_post(
    post_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> BlogPostRead:
    row = await blog_service.admin_get(db, post_id)
    return BlogPostRead.model_validate(row)


@router.patch(
    "/blog/{post_id}", response_model=BlogPostRead, summary="Update a blog post"
)
async def update_post(
    post_id: uuid.UUID,
    payload: BlogPostUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> BlogPostRead:
    row = await blog_service.update(db, post_id, payload)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.update",
        target_table="blog_posts",
        target_id=row.id,
        payload=payload.model_dump(exclude_unset=True, mode="json"),
    )
    return BlogPostRead.model_validate(row)


@router.delete(
    "/blog/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a blog post",
)
async def delete_post(
    post_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> None:
    await blog_service.delete(db, post_id)
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="blog.delete",
        target_table="blog_posts",
        target_id=post_id,
    )


__all__ = ["router"]
