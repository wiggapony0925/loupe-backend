"""Schemas for the admin "Ask your data" natural-language query tool."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AskResponse(BaseModel):
    # False when no ANTHROPIC_API_KEY is set — the UI shows a setup hint.
    configured: bool
    question: str
    # The generated SQL (shown for transparency), or None when unconfigured/failed.
    sql: str | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    row_count: int = 0
    truncated: bool = False
    error: str | None = None


__all__ = ["AskRequest", "AskResponse"]
