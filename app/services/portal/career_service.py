"""Careers — job-posting CRUD, public applications, and the applicant pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.career import ApplicationEvent, JobApplication, JobPosting
from app.models.enums import ApplicationStatusEnum, JobStatusEnum
from app.models.user import User
from app.schemas.portal import (
    ApplicationStatusUpdate,
    ApplicationSubmitted,
    ApplicationTrackRead,
    JobApplicationCreate,
    JobApplicationDetail,
    JobApplicationRead,
    JobPostingCreate,
    JobPostingUpdate,
    slugify,
)
from app.services.portal import notifications

# ── Jobs ────────────────────────────────────────────────────────────────


async def _unique_slug(
    db: AsyncSession, base: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    base = slugify(base)
    candidate = base
    n = 1
    while True:
        stmt = select(JobPosting.id).where(JobPosting.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(JobPosting.id != exclude_id)
        if (await db.execute(stmt)).first() is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


async def list_open(db: AsyncSession) -> list[JobPosting]:
    rows = (
        (
            await db.execute(
                select(JobPosting)
                .where(JobPosting.status == JobStatusEnum.open.value)
                .order_by(JobPosting.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def get_open_by_slug(db: AsyncSession, slug: str) -> JobPosting:
    row = (
        await db.execute(
            select(JobPosting).where(
                JobPosting.slug == slug,
                JobPosting.status == JobStatusEnum.open.value,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    return row


async def admin_list_jobs(db: AsyncSession) -> list[JobPosting]:
    rows = (
        (await db.execute(select(JobPosting).order_by(JobPosting.created_at.desc())))
        .scalars()
        .all()
    )
    return list(rows)


async def admin_get_job(db: AsyncSession, job_id: uuid.UUID) -> JobPosting:
    row = (
        await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    return row


async def create_job(db: AsyncSession, payload: JobPostingCreate) -> JobPosting:
    row = JobPosting(
        slug=await _unique_slug(db, payload.slug or payload.title),
        title=payload.title,
        team=payload.team,
        location=payload.location,
        employment_type=payload.employment_type.value,
        summary=payload.summary,
        description=payload.description,
        status=payload.status.value,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_job(
    db: AsyncSession, job_id: uuid.UUID, payload: JobPostingUpdate
) -> JobPosting:
    row = await admin_get_job(db, job_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("slug"):
        row.slug = await _unique_slug(db, data.pop("slug"), exclude_id=row.id)
    else:
        data.pop("slug", None)
    for key, value in data.items():
        # Enum fields arrive as Enum instances → store their value.
        setattr(row, key, value.value if hasattr(value, "value") else value)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_job(db: AsyncSession, job_id: uuid.UUID) -> None:
    row = await admin_get_job(db, job_id)
    await db.delete(row)
    await db.commit()


# ── Applications ────────────────────────────────────────────────────────


async def apply(
    db: AsyncSession, job_id: uuid.UUID, payload: JobApplicationCreate
) -> ApplicationSubmitted:
    """Submit an application to an *open* role. Records the opening event."""
    job = (
        await db.execute(select(JobPosting).where(JobPosting.id == job_id))
    ).scalar_one_or_none()
    if job is None or job.status != JobStatusEnum.open.value:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not open"
        )

    application = JobApplication(
        job_id=job.id,
        applicant_name=payload.applicant_name,
        applicant_email=str(payload.applicant_email).strip().lower(),
        linkedin_url=payload.linkedin_url,
        resume_url=payload.resume_url,
        cover_letter=payload.cover_letter,
        status=ApplicationStatusEnum.submitted.value,
    )
    db.add(application)
    await db.flush()
    db.add(
        ApplicationEvent(
            application_id=application.id,
            status=ApplicationStatusEnum.submitted.value,
            message="Application received. We'll be in touch.",
            notified=False,
        )
    )
    await db.commit()
    await db.refresh(application)
    return ApplicationSubmitted(
        id=application.id,
        status=ApplicationStatusEnum(application.status),
        job_title=job.title,
        created_at=application.created_at,
    )


async def track(
    db: AsyncSession, application_id: uuid.UUID, email: str
) -> ApplicationTrackRead:
    """Public: an applicant's own view of their application, gated on email."""
    row = (
        await db.execute(
            select(JobApplication, JobPosting.title)
            .join(JobPosting, JobPosting.id == JobApplication.job_id)
            .options(selectinload(JobApplication.events))
            .where(JobApplication.id == application_id)
        )
    ).first()
    if row is None or row[0].applicant_email != email.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    application, job_title = row
    return ApplicationTrackRead(
        id=application.id,
        job_title=job_title,
        applicant_name=application.applicant_name,
        status=ApplicationStatusEnum(application.status),
        created_at=application.created_at,
        events=list(application.events),
    )


async def admin_list_applications(
    db: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    status_filter: str | None = None,
) -> list[JobApplicationRead]:
    stmt = (
        select(JobApplication, JobPosting.title)
        .join(JobPosting, JobPosting.id == JobApplication.job_id)
        .order_by(JobApplication.created_at.desc())
    )
    if job_id is not None:
        stmt = stmt.where(JobApplication.job_id == job_id)
    if status_filter:
        stmt = stmt.where(JobApplication.status == status_filter)
    rows = (await db.execute(stmt)).all()
    out: list[JobApplicationRead] = []
    for app_row, job_title in rows:
        read = JobApplicationRead.model_validate(app_row)
        read.job_title = job_title
        out.append(read)
    return out


async def admin_get_application(
    db: AsyncSession, application_id: uuid.UUID
) -> JobApplicationDetail:
    row = (
        await db.execute(
            select(JobApplication, JobPosting.title)
            .join(JobPosting, JobPosting.id == JobApplication.job_id)
            .options(selectinload(JobApplication.events))
            .where(JobApplication.id == application_id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    application, job_title = row
    detail = JobApplicationDetail.model_validate(application)
    detail.job_title = job_title
    return detail


async def update_application_status(
    db: AsyncSession,
    user: User,
    application_id: uuid.UUID,
    payload: ApplicationStatusUpdate,
) -> JobApplicationDetail:
    """Advance an application, record the event, and (best-effort) notify."""
    application = (
        await db.execute(
            select(JobApplication).where(JobApplication.id == application_id)
        )
    ).scalar_one_or_none()
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )

    application.status = payload.status.value
    application.updated_at = datetime.now(UTC)

    event = ApplicationEvent(
        application_id=application.id,
        status=payload.status.value,
        message=payload.message,
        created_by_user_id=user.id,
        notified=False,
    )
    if payload.notify:
        event.notified = await notifications.notify_applicant(application, event)
    db.add(event)
    await db.commit()
    return await admin_get_application(db, application_id)


__all__ = [
    "admin_get_application",
    "admin_get_job",
    "admin_list_applications",
    "admin_list_jobs",
    "apply",
    "create_job",
    "delete_job",
    "get_open_by_slug",
    "list_open",
    "track",
    "update_application_status",
    "update_job",
]
