"""Sanity tests for the orchestrator-side `process_scan` worker."""

import uuid

import pytest
from sqlalchemy import select

from app.models.enums import ScanStatusEnum
from app.models.scan import ScanJob
from app.tasks.scan_processor import process_scan


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


@pytest.mark.asyncio
async def test_a_crashing_scan_is_marked_failed_instead_of_hanging(
    db_session, created_user, monkeypatch
):
    """The regression: a crash left the job in `processing` forever.

    `_process` had no try/except, the router's caller swallows what escapes,
    and no cron reaps stuck jobs — so the client, which polls for
    complete-or-failed, waited indefinitely with nothing to show the user.
    It also made the pipeline undiagnosable: a systematically failing scan
    looked exactly like nobody scanning.
    """
    from app.tasks import scan_processor

    job = ScanJob(
        user_id=created_user.id,
        status=ScanStatusEnum.uploading,
        images_s3_keys={"front": "scans/front.jpg"},
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    def _boom(*args, **kwargs):
        raise RuntimeError("grading backend exploded")

    monkeypatch.setattr(scan_processor, "grade_from_images", _boom)

    result = await process_scan(
        {"job_id": str(job_id), "user_id": str(created_user.id)}, db=db_session
    )
    assert result["status"] == "failed"

    row = (
        await db_session.execute(select(ScanJob).where(ScanJob.id == job_id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.status == ScanStatusEnum.failed, (
        f"job is still {row.status} — the client would poll forever"
    )
    assert row.error_message and "RuntimeError" in row.error_message
    assert row.completed_at is not None
