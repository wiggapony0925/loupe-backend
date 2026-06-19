"""Schemas for the Loupe Scanner waitlist.

Public surface: join the waitlist (the "checkout" button on the scanner
product page) and read aggregate stats. Admin surface: list every signup
and advance a signup's status. ``status`` validates against
:class:`~app.models.enums.WaitlistStatusEnum` but stores its string value.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import WaitlistStatusEnum

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CTRL_RE.sub("", value).strip()
    return cleaned or None


class WaitlistJoin(BaseModel):
    """Public: join the scanner waitlist (the checkout CTA payload)."""

    email: EmailStr
    name: str | None = Field(None, max_length=160)
    interest: str | None = Field(None, max_length=2000)
    referral_source: str | None = Field(None, max_length=120)
    quantity: int = Field(1, ge=1, le=10)

    @field_validator("name", "interest", "referral_source")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)


class WaitlistJoined(BaseModel):
    """Public response after joining — the reference + the line position."""

    id: uuid.UUID
    email: EmailStr
    status: WaitlistStatusEnum
    # 1-based place in line (waiting signups only). Lets the UI say
    # "You're #128 in line."
    position: int
    created_at: datetime


class WaitlistStats(BaseModel):
    """Public aggregate — drives the "Join N collectors" social proof."""

    total: int
    waiting: int


class WaitlistEntryRead(BaseModel):
    """Admin: one waitlist signup row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str | None = None
    interest: str | None = None
    referral_source: str | None = None
    user_id: uuid.UUID | None = None
    quantity: int
    status: WaitlistStatusEnum
    created_at: datetime
    updated_at: datetime


class WaitlistStatusUpdate(BaseModel):
    """Admin: advance a signup's pipeline stage."""

    status: WaitlistStatusEnum


__all__ = [
    "WaitlistEntryRead",
    "WaitlistJoin",
    "WaitlistJoined",
    "WaitlistStats",
    "WaitlistStatusUpdate",
]
