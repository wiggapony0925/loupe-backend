"""Derived market analytics for a card — composed from the market snapshot.

`GET /v1/cards/{id}/analytics` returns this public, read-only view: figures
*derived* from the card's market snapshot + recent sold comps (market cap,
momentum, volatility, grade premium, all-time high/low, liquidity), so every
client renders the same numbers instead of each recomputing them. Pure
derivation — no new data sources.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CardAnalytics(BaseModel):
    """Derived market metrics for one card (public, read-only)."""

    model_config = ConfigDict(extra="forbid")

    card_id: str

    # ── Valuation ──
    market_price_usd: Decimal | None = None
    graded_avg_usd: Decimal | None = None
    population: int = 0
    # Value of the whole graded population (graded_avg x population).
    market_cap_usd: Decimal | None = None

    # ── Momentum — signed % change over each trailing window ──
    momentum_7d: float | None = None
    momentum_30d: float | None = None
    momentum_90d: float | None = None
    momentum_1y: float | None = None

    # ── Risk & quality ──
    # 90-day coefficient of variation (stdev / mean, as %).
    volatility_pct: float | None = None
    # Top-grade / raw price (e.g. 8.2 = PSA 10 worth 8.2x the raw card).
    grade_premium: float | None = None

    # ── Extremes (all-time within available history) ──
    all_time_high_usd: Decimal | None = None
    all_time_low_usd: Decimal | None = None
    # Current vs ATH, signed % (<= 0 = below the peak).
    pct_off_ath: float | None = None

    # ── Liquidity ──
    # Count of sold comps in the trailing 30 days.
    liquidity_30d: int = 0


__all__ = ["CardAnalytics"]
