"""User-facing Pydantic schemas (Read / Update + settings)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRead(BaseModel):
    """Public representation of a user (e.g. ``GET /me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime


class UserUpdate(BaseModel):
    """Allowed mutations on the caller's own user record."""

    display_name: str | None = Field(None, max_length=120)
    avatar_url: str | None = Field(None, max_length=1024)


class UserSettingsRead(BaseModel):
    """Per-user settings as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    currency: str = "USD"
    theme: str = "system"
    live_sync_enabled: bool = True
    push_notifications_enabled: bool = True
    updated_at: datetime | None = None


class UserSettingsUpdate(BaseModel):
    """Allowed mutations on user settings."""

    currency: str | None = Field(default=None, min_length=3, max_length=3)
    theme: str | None = Field(default=None, pattern="^(system|light|dark)$")
    live_sync_enabled: bool | None = None
    push_notifications_enabled: bool | None = None


__all__ = ["UserRead", "UserSettingsRead", "UserSettingsUpdate", "UserUpdate"]
