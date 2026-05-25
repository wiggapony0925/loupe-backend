"""Scan-job orchestration: create + presigned URLs, complete, list, delete."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.s3 import get_s3_client
from app.config import get_settings
from app.models.enums import ScanStatusEnum
from app.models.scan import ScanJob
from app.models.user import User
from app.schemas.scan import (
    PresignedUpload,
    ScanAngle,
    ScanJobCompleteRequest,
    ScanJobCreate,
)
from app.utils.logger import get_logger
from app.utils.time import utcnow

logger = get_logger("services.scan")


def _build_s3_key(user_id: uuid.UUID, job_id: uuid.UUID, angle: ScanAngle) -> str:
    return f"scans/{user_id}/{job_id}/{angle}.jpg"


async def create_job(
    db: AsyncSession, user: User, payload: ScanJobCreate
) -> tuple[ScanJob, list[PresignedUpload]]:
    """Persist a new ``ScanJob`` and issue presigned PUT URLs for each angle."""
    s = get_settings()
    job = ScanJob(
        user_id=user.id,
        scanner_id=payload.scanner_id,
        status=ScanStatusEnum.uploading,
        source=payload.source,
        images_s3_keys={},
    )
    db.add(job)
    await db.flush()

    s3 = get_s3_client()
    uploads: list[PresignedUpload] = []
    keys: dict[str, str] = {}
    for angle in payload.angles:
        key = _build_s3_key(user.id, job.id, angle)
        url = await s3.generate_presigned_put_url(
            bucket=s.s3_bucket,
            key=key,
            content_type="image/jpeg",
            expires_in=s.s3_presign_expires_seconds,
        )
        uploads.append(
            PresignedUpload(
                angle=angle,
                upload_url=url,
                s3_key=key,
                expires_in=s.s3_presign_expires_seconds,
            )
        )
        keys[angle] = key

    job.images_s3_keys = keys
    await db.commit()
    await db.refresh(job)
    return job, uploads


async def list_for_user(
    db: AsyncSession, user: User, *, limit: int = 50
) -> list[ScanJob]:
    rows = await db.execute(
        select(ScanJob)
        .where(ScanJob.user_id == user.id)
        .order_by(ScanJob.created_at.desc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def get_for_user(
    db: AsyncSession, user: User, job_id: uuid.UUID
) -> ScanJob | None:
    return (
        await db.execute(
            select(ScanJob).where(ScanJob.id == job_id, ScanJob.user_id == user.id)
        )
    ).scalar_one_or_none()


async def mark_complete(
    db: AsyncSession,
    user: User,
    job_id: uuid.UUID,
    payload: ScanJobCompleteRequest,
) -> ScanJob | None:
    """Transition a job to ``processing`` once the client has uploaded all angles."""
    job = await get_for_user(db, user, job_id)
    if job is None:
        return None
    if job.status not in (ScanStatusEnum.uploading, ScanStatusEnum.queued):
        return job
    expected = set((job.images_s3_keys or {}).keys())
    missing = expected - set(payload.uploaded_angles)
    if missing:
        logger.warning("Scan %s missing angles: %s", job.id, sorted(missing))
    job.status = ScanStatusEnum.processing
    job.started_at = utcnow()
    await db.commit()
    await db.refresh(job)
    return job


async def delete(db: AsyncSession, user: User, job_id: uuid.UUID) -> bool:
    job = await get_for_user(db, user, job_id)
    if job is None:
        return False
    await db.delete(job)
    await db.commit()
    return True


__all__ = [
    "create_job",
    "delete",
    "get_for_user",
    "list_for_user",
    "mark_complete",
]
