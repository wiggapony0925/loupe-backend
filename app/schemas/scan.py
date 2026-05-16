"""Scan-job schemas — ingest workflow + WebSocket progress events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ScanSourceEnum, ScanStatusEnum

#: The four camera angles the hardware scanner captures.
ScanAngle = Literal["front", "back", "top", "bottom"]
ALL_ANGLES: tuple[ScanAngle, ...] = ("front", "back", "top", "bottom")


class ScanJobCreate(BaseModel):
    """Body for ``POST /v1/scans`` — request a new scan + presigned uploads."""

    scanner_id: uuid.UUID | None = Field(
        None, description="Originating scanner; null for phone scans."
    )
    source: ScanSourceEnum = ScanSourceEnum.phone
    angles: list[ScanAngle] = Field(
        default_factory=lambda: list(ALL_ANGLES),
        min_length=1,
        max_length=4,
        description="Subset of card angles to upload.",
    )


class PresignedUpload(BaseModel):
    """A single presigned PUT URL the client uses to upload one angle."""

    angle: ScanAngle
    upload_url: str
    s3_key: str
    expires_in: int


class ScanJobRead(BaseModel):
    """API representation of a scan job."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    scanner_id: uuid.UUID | None = None
    status: ScanStatusEnum
    source: ScanSourceEnum
    images_s3_keys: dict[str, str] | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ScanJobCreateResponse(BaseModel):
    """Response from ``POST /v1/scans`` — job + presigned URLs."""

    job: ScanJobRead
    uploads: list[PresignedUpload]


class ScanJobCompleteRequest(BaseModel):
    """Body for ``POST /v1/scans/{id}/complete``."""

    uploaded_angles: list[ScanAngle] = Field(..., min_length=1, max_length=4)


class ScanProgressEvent(BaseModel):
    """A WebSocket frame pushed for live scan progress."""

    type: Literal["scan_progress"] = "scan_progress"
    job_id: uuid.UUID
    status: ScanStatusEnum
    progress: float = Field(..., ge=0.0, le=1.0)
    message: str | None = None
    result: dict | None = None


__all__ = [
    "ALL_ANGLES",
    "PresignedUpload",
    "ScanAngle",
    "ScanJobCompleteRequest",
    "ScanJobCreate",
    "ScanJobCreateResponse",
    "ScanJobRead",
    "ScanProgressEvent",
]
