"""Schemas for the admin grade-review queue (QA of first-party Loupe grades)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class GradeReviewRow(BaseModel):
    id: uuid.UUID
    user_email: str | None = None
    card_name: str | None = None
    card_image_url: str | None = None
    set_name: str | None = None
    house: str
    grade: float
    subgrades: dict[str, Any] | None = None
    condition: str | None = None
    estimated_value_usd: float | None = None
    acquired_via: str | None = None
    graded_at: datetime


class GradeReviewPage(BaseModel):
    results: list[GradeReviewRow]
    total: int
    page: int
    page_size: int
    # Houses present in the data, for the filter dropdown.
    houses: list[str]


__all__ = ["GradeReviewPage", "GradeReviewRow"]
