"""Sanity tests for the orchestrator-side `process_scan` worker."""

import uuid

import pytest

from app.models.enums import ScanStatusEnum
from app.models.scan import ScanJob
from app.workers.scan_processor import process_scan


@pytest.mark.asyncio
async def test_process_scan_marks_complete(db_session, created_user):
    job = ScanJob(
        user_id=created_user.id,
        status=ScanStatusEnum.uploading,
        images_s3_keys={
            "front": "scans/test/front.jpg",
            "back": "scans/test/back.jpg",
        },
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    await process_scan(
        {"job_id": str(job.id), "user_id": str(created_user.id)}, db=db_session
    )
    await db_session.refresh(job)
    assert job.status == "complete"


@pytest.mark.asyncio
async def test_process_scan_missing_job_is_noop(db_session, created_user):
    await process_scan(
        {"job_id": str(uuid.uuid4()), "user_id": str(created_user.id)}, db=db_session
    )
