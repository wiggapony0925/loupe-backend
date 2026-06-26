"""Derived market analytics — compose figures from the market snapshot.

Read-only and public. Reuses :func:`market_service.get_card_market` (so it
shares the same stable price model the rest of the card page uses) plus the
sold-comps service, then *derives* market cap, momentum, volatility, grade
premium, all-time high/low, and liquidity. No new data sources — every figure
is a pure transform of data the card already exposes, computed once on the
server so web + mobile show identical numbers.
"""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from app.schemas.card_analytics import CardAnalytics
from app.services.market import market_service, sold_comps_service


def _amount(money: Any) -> float | None:
    """Pull a float amount out of a `{amount, currency}` money dict."""
    if isinstance(money, dict) and money.get("amount") is not None:
        try:
            return float(money["amount"])
        except (TypeError, ValueError):
            return None
    return None


def _usd(v: float | None) -> Decimal | None:
    return Decimal(str(round(v, 2))) if v is not None else None


def _volatility(points: list[dict[str, Any]]) -> float | None:
    """Coefficient of variation (stdev / mean, as %) over a price series."""
    prices = [float(p["price"]) for p in points if p.get("price") is not None]
    if len(prices) < 2:
        return None
    mean = statistics.fmean(prices)
    if mean <= 0:
        return None
    return round(statistics.pstdev(prices) / mean * 100, 2)


async def get_card_analytics(card_id: str) -> CardAnalytics | None:
    """Derive the public analytics view for a card, or None if it doesn't exist."""
    market = await market_service.get_card_market(card_id)
    if market is None:
        return None

    snap = market.get("snapshot") or {}
    summary = snap.get("summary") or {}
    history = snap.get("history") or {}

    raw = _amount(summary.get("raw"))
    graded_avg = _amount(summary.get("graded_avg"))
    pop_top = _amount(summary.get("pop_top"))
    population = int(summary.get("pop_total") or 0)

    market_cap = (
        graded_avg * population if (graded_avg is not None and population) else None
    )
    grade_premium = round(pop_top / raw, 2) if (pop_top is not None and raw) else None

    def momentum(rng: str) -> float | None:
        cp = ((history.get(rng) or {}).get("summary") or {}).get("change_pct")
        return float(cp) if cp is not None else None

    volatility = _volatility(((history.get("90d") or {}).get("points")) or [])

    all_summary = (history.get("all") or {}).get("summary") or {}
    ath = all_summary.get("max")
    atl = all_summary.get("min")
    current = all_summary.get("current")
    if current is None:
        current = raw
    pct_off_ath = (
        round((current - ath) / ath * 100, 2)
        if (ath and current is not None and ath > 0)
        else None
    )

    comps = await sold_comps_service.get_comps_for_card(card_id, days=30)
    liquidity_30d = len((comps or {}).get("comps") or [])

    return CardAnalytics(
        card_id=card_id,
        market_price_usd=_usd(raw),
        graded_avg_usd=_usd(graded_avg),
        population=population,
        market_cap_usd=_usd(market_cap),
        momentum_7d=momentum("7d"),
        momentum_30d=momentum("30d"),
        momentum_90d=momentum("90d"),
        momentum_1y=momentum("1y"),
        volatility_pct=volatility,
        grade_premium=grade_premium,
        all_time_high_usd=_usd(ath),
        all_time_low_usd=_usd(atl),
        pct_off_ath=pct_off_ath,
        liquidity_30d=liquidity_30d,
    )


__all__ = ["get_card_analytics"]
