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


class ReportDownloadResponse(BaseModel):
    """Response for ``GET /v1/reports/{id}/download``.

    `download_url` is a short-lived presigned URL when the storage
    backend supports it. When it's ``None``, the client must fall back
    to the streaming endpoint (`GET /v1/reports/{id}/file`).
    """

    download_url: str | None = None
    expires_in_seconds: int


__all__ = [
    "ReportDownloadResponse",
    "ReportGenerateRequest",
    "ReportRead",
]
