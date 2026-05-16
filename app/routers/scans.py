"""Scan ingestion endpoints + worker enqueue."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.db import get_db
from app.models.user import User
from app.schemas.scan import (
    ScanJobCompleteRequest,
    ScanJobCreate,
    ScanJobCreateResponse,
    ScanJobRead,
)
from app.services import scan_service
from app.utils.logger import get_logger

router = APIRouter(prefix="/scans", tags=["scans"])
logger = get_logger("routers.scans")


@router.post(
    "",
    response_model=ScanJobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new scan job and return presigned upload URLs",
)
async def create_scan(
    payload: ScanJobCreate,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScanJobCreateResponse:
    job, uploads = await scan_service.create_job(db, user, payload)
    return ScanJobCreateResponse(job=ScanJobRead.model_validate(job), uploads=uploads)


@router.post(
    "/{job_id}/complete",
    response_model=ScanJobRead,
    summary="Mark uploads complete; enqueue grading worker",
)
async def complete_scan(
    job_id: uuid.UUID,
    payload: ScanJobCompleteRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScanJobRead:
    job = await scan_service.mark_complete(db, user, job_id, payload)
    if job is None:
        raise HTTPException(status_code=404, detail="Scan job not found")
    # Best-effort enqueue: in dev/test we just call the worker inline.
    try:
        from app.workers.scan_processor import process_scan  # local import

        await process_scan({"job_id": str(job.id), "user_id": str(user.id)}, db=db)
    except Exception as exc:  # pragma: no cover - background path
        logger.warning("Inline scan processing failed (will rely on worker): %s", exc)
    await db.refresh(job)
    return ScanJobRead.model_validate(job)


@router.get("", response_model=list[ScanJobRead], summary="List my scans")
async def list_scans(
    user: User = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> list[ScanJobRead]:
    rows = await scan_service.list_for_user(db, user)
    return [ScanJobRead.model_validate(r) for r in rows]


@router.get("/{job_id}", response_model=ScanJobRead, summary="Get one scan")
async def get_scan(
    job_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ScanJobRead:
    job = await scan_service.get_for_user(db, user, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Scan job not found")
    return ScanJobRead.model_validate(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scan(
    job_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await scan_service.delete(db, user, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scan job not found")


__all__ = ["router"]
