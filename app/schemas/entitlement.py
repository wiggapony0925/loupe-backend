"""Entitlement schemas — the signed-in user's effective Loupe Pro access.

The client never decides what's unlocked; it reads this computed payload and
gates UI on it. Snake_case to match ``UserRead`` (this is a ``/me`` sub-resource).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PlanLimits(BaseModel):
    """Hard numeric caps. ``None`` means unlimited."""

    max_cards: int | None = None
    # Statement PDFs a free user may download (their latest N). Pro = unlimited.
    free_statements: int | None = None


class PlanFeatures(BaseModel):
    """Boolean capability gates the UI reads to lock/unlock surfaces."""

    unlimited_cards: bool
    scanner_import: bool
    full_history: bool
    unlimited_alerts: bool
    statements: bool
    pro_badge: bool
    # Semantic "describe it" AI search ("red lizard with fire" → Charizard).
    ai_search: bool = False


class EntitlementsRead(BaseModel):
    """Everything the client needs to render gates, badges, and the paywall."""

    plan: str  # "free" | "pro"
    is_pro: bool
    # True while Pro access is a free trial (Stripe `trialing`).
    trialing: bool = False
    # The global kill switch. When False the entitlement layer treats everyone
    # as Pro — the client should hide the paywall and all upgrade CTAs.
    subscriptions_enabled: bool
    pro_since: datetime | None = None
    pro_expires_at: datetime | None = None
    # Live count of the user's owned cards (drives the "X of 50" meter).
    card_count: int
    limits: PlanLimits
    features: PlanFeatures


__all__ = ["EntitlementsRead", "PlanFeatures", "PlanLimits"]
