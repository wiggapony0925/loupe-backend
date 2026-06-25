"""Schemas for the admin revenue analytics surface.

Loupe Pro subscription state lives on the ``User`` row (there is no separate
billing table), so these are derived metrics. Money figures are clearly
*estimates*: we know who is paying but not their billing interval, so MRR is
modelled at the monthly price.
"""

from __future__ import annotations

from pydantic import BaseModel


class RevenueMonthPoint(BaseModel):
    """New Pro members that started in a given month (YYYY-MM)."""

    month: str
    new_pro: int


class RevenueSummary(BaseModel):
    billing_configured: bool
    currency: str = "USD"

    # ── Subscriber mix ──
    paying: int  # Pro via an active Stripe subscription (not trialing)
    trialing: int  # in a Stripe free trial
    comped: int  # Pro by admin grant (no Stripe subscription)
    free: int
    total_users: int

    # ── Money (estimates) ──
    price_monthly_usd: float
    price_yearly_usd: float
    est_mrr_usd: float
    est_arr_usd: float

    # ── Movement (trailing 30 days) ──
    new_pro_30d: int
    churned_30d: int
    churn_rate_30d: float

    # ── Trend ──
    pro_by_month: list[RevenueMonthPoint]


__all__ = ["RevenueMonthPoint", "RevenueSummary"]
