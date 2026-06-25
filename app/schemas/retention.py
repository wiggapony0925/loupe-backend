"""Schemas for the admin cohort-retention triangle.

A user is "active in week N" of their cohort if they scanned or added a card in
that week (the same activity proxy the engagement page uses).
"""

from __future__ import annotations

from pydantic import BaseModel


class CohortRow(BaseModel):
    # ISO date of the cohort's signup week (Monday), e.g. "2026-05-04".
    cohort: str
    size: int
    # retention[N] = fraction of the cohort active in week N (0 = signup week).
    # Length is capped at the number of weeks elapsed since the cohort.
    retention: list[float]


class RetentionReport(BaseModel):
    weeks: int
    cohorts: list[CohortRow]


__all__ = ["CohortRow", "RetentionReport"]
