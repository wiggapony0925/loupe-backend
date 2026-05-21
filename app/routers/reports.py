"""Portfolio-statement endpoints (`/v1/reports`).

Lets a signed-in user generate, list, and download monthly / yearly
PDF statements of their collection. Reports are attached to the user
in the database and persist for the lifetime of the account.

The router is intentionally small — all heavy lifting lives in
`app.services.reports`. Endpoints:

* ``POST /v1/reports``                 — generate (or reuse) a statement
* ``GET  /v1/reports``                 — list all statements for the user
* ``GET  /v1/reports/{id}``            — fetch one statement's metadata
* ``GET  /v1/reports/{id}/download``   — short-lived presigned URL
* ``GET  /v1/reports/{id}/file``       — stream the PDF directly
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_user
from app.config import get_settings
from app.db import get_db
from app.models.enums import ReportStatusEnum
from app.models.user import User
from app.schemas.report import (
    ReportDownloadResponse,
    ReportGenerateRequest,
    ReportRead,
)
from app.services.reports import (
    generate_report,
    get_report,
    list_reports,
)
from app.services.reports.storage import (
    download_report_bytes,
    generate_presigned_download_url,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=list[ReportRead], summary="List my statements")
async def list_mine(
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> list[ReportRead]:
    rows = await list_reports(db, user)
    return [ReportRead.model_validate(r) for r in rows]


@router.post(
    "",
    response_model=ReportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a monthly or yearly statement",
)
async def create(
    payload: ReportGenerateRequest,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    try:
        row = await generate_report(
            db,
            user=user,
            period=payload.period,
            year=payload.year,
            month=payload.month,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ReportRead.model_validate(row)


@router.get("/{report_id}", response_model=ReportRead, summary="Get statement metadata")
async def read_one(
    report_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ReportRead:
    row = await get_report(db, user, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return ReportRead.model_validate(row)


@router.get(
    "/{report_id}/download",
    response_model=ReportDownloadResponse,
    summary="Short-lived presigned URL for the PDF",
)
async def download_url(
    report_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> ReportDownloadResponse:
    row = await get_report(db, user, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if row.status is not ReportStatusEnum.ready or not row.storage_key:
        raise HTTPException(status_code=409, detail="Report is not ready")
    s = get_settings()
    url = await generate_presigned_download_url(
        row.storage_key, expires_in=s.s3_presign_expires_seconds
    )
    return ReportDownloadResponse(
        download_url=url,
        expires_in_seconds=s.s3_presign_expires_seconds,
    )


@router.get(
    "/{report_id}/file",
    summary="Stream the PDF (fallback when presigning isn't available)",
    response_class=Response,
)
async def stream_file(
    report_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    row = await get_report(db, user, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if row.status is not ReportStatusEnum.ready or not row.storage_key:
        raise HTTPException(status_code=409, detail="Report is not ready")
    body = await download_report_bytes(row.storage_key)
    if body is None:
        # Storage object disappeared underneath us — flag the row so the
        # next generate attempt rebuilds it instead of returning 500s.
        row.status = ReportStatusEnum.failed
        row.error_message = "PDF missing from storage"
        await db.commit()
        raise HTTPException(status_code=410, detail="Report PDF is no longer available")
    safe_name = row.title.replace(" ", "_").replace("/", "_")
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.pdf"',
            "Content-Length": str(len(body)),
        },
    )


__all__ = ["router"]
