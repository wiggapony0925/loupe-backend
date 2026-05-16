"""Scanner device schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ScannerTransportEnum


class ScannerRead(BaseModel):
    """A registered scanner as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str
    name: str | None = None
    firmware_version: str | None = None
    transport: ScannerTransportEnum
    is_active: bool
    last_seen_at: datetime | None = None
    created_at: datetime


class ScannerCreate(BaseModel):
    """Body for ``POST /v1/scanners`` (pair new device)."""

    device_id: str = Field(..., min_length=4, max_length=128)
    name: str | None = Field(None, max_length=120)
    firmware_version: str | None = Field(None, max_length=40)
    transport: ScannerTransportEnum = ScannerTransportEnum.wifi


class ScannerUpdate(BaseModel):
    """Body for ``PATCH /v1/scanners/{id}``."""

    name: str | None = Field(None, max_length=120)
    firmware_version: str | None = Field(None, max_length=40)
    transport: ScannerTransportEnum | None = None
    is_active: bool | None = None


class ScannerHeartbeat(BaseModel):
    """Body for ``POST /v1/scanners/{id}/heartbeat``."""

    firmware_version: str | None = Field(None, max_length=40)


__all__ = ["ScannerCreate", "ScannerHeartbeat", "ScannerRead", "ScannerUpdate"]
