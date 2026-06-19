"""Feature-flag schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class FeatureFlagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    description: str | None = None
    enabled: bool
    updated_at: datetime | None = None


class FeatureFlagCreate(BaseModel):
    key: str = Field(
        ..., max_length=80, description="Stable identifier, e.g. `web_markets`."
    )
    label: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    enabled: bool = False

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip().lower()
        if not _KEY_RE.match(v):
            raise ValueError(
                "key must be lowercase letters, digits, and underscores (start with a letter)"
            )
        return v


class FeatureFlagUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=2000)
    enabled: bool | None = None


__all__ = ["FeatureFlagCreate", "FeatureFlagRead", "FeatureFlagUpdate"]
