"""Schemas for the admin live activity feed (pulse)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

PulseType = Literal["signup", "scan", "acquisition", "admin"]


class PulseEvent(BaseModel):
    # Stable composite id ("type:uuid") so the client can key the list.
    id: str
    type: PulseType
    at: datetime
    actor: str | None = None
    title: str
    detail: str | None = None
    value_usd: float | None = None


class PulseFeed(BaseModel):
    events: list[PulseEvent]


__all__ = ["PulseEvent", "PulseFeed", "PulseType"]
