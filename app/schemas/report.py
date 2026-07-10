"""Schemas for the user-facing reports API."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ReportPeriodEnum, ReportStatusEnum


class ReportRead(BaseModel):
    """Public representation of a generated portfolio statement."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period: ReportPeriodEnum
    period_start: date
    period_end: date
    status: ReportStatusEnum
    title: str
    # Collection scope; null = whole-vault statement. `collection_name`
    # is baked at generation time so the label survives collection
    # rename/delete.
    collection_id: uuid.UUID | None = None
    collection_name: str | None = None
    file_size_bytes: int | None = None
    error_message: str | None = None
    generated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReportGenerateRequest(BaseModel):
    """Body for ``POST /v1/reports`` — generate a new statement."""

    model_config = ConfigDict(extra="forbid")

    period: ReportPeriodEnum
    year: int = Field(..., ge=1900, le=2100)
    # Only required for monthly reports. Validated server-side because
    # FastAPI can't express the dependency in JSON schema.
    month: int | None = Field(None, ge=1, le=12)
    # Scope the statement to one collection. Omit / null = whole vault.
    collection_id: uuid.UUID | None = None


class ReportDownloadResponse(BaseModel):
    """Response for ``GET /v1/reports/{id}/download``.

    `download_url` is a short-lived presigned URL when the storage
    backend supports it. When it's ``None``, the client must fall back
    to the streaming endpoint (`GET /v1/reports/{id}/file`).
    """

    download_url: str | None = None
    expires_in_seconds: int


class UpcomingReportRead(BaseModel):
    """When the next monthly / yearly statement will auto-materialise.

    Returned by ``GET /v1/reports/upcoming`` so the UI can render an
    Amex-style "Your next statement closes on Jun 1" line without the
    client doing date math against the user's timezone.
    """

    period: ReportPeriodEnum
    # Inclusive period currently in progress (the one that will close).
    period_start: date
    period_end: date
    # UTC datetime when the statement will become available.
    closes_at: datetime
    # Human-readable label for the in-progress period, e.g. "May 2026".
    label: str


__all__ = [
    "ReportDownloadResponse",
    "ReportGenerateRequest",
    "ReportRead",
    "UpcomingReportRead",
]
