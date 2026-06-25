"""Schemas for the admin engagement / retention analytics surface.

"Active" is a proxy: a user counts as active in a window if they scanned a card
or added one to a collection within it (there is no separate sessions/events
table). "Activated" means they have added at least one card.
"""

from __future__ import annotations

from pydantic import BaseModel


class WeekPoint(BaseModel):
    # ISO date of the week's Monday (e.g. "2026-06-22").
    week: str
    new_users: int


class FunnelStep(BaseModel):
    label: str
    count: int


class EngagementSummary(BaseModel):
    total_users: int
    active_7d: int
    active_30d: int
    active_90d: int

    activated_users: int
    activation_rate: float
    pro_users: int
    pro_rate: float

    new_users_by_week: list[WeekPoint]
    funnel: list[FunnelStep]


__all__ = ["EngagementSummary", "FunnelStep", "WeekPoint"]
