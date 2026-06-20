"""Card valuation — one equilibrium "fair value" plus the per-grade ladder.

Most price sites throw a wall of numbers at you (last sale, low, market, each
grade) and leave you to guess what the card is "really" worth. We add the
missing piece: a single, defensible **Loupe Value** for the raw card — the
economic equilibrium where the signals we have actually agree.

We blend, weighted by how closely each reflects a real clearing price:

    sold comps (actual transactions)  >  live listings (the ask side)  >  catalog market

…re-normalised over whichever signals are present, and we surface the inputs so
the number is transparent (not a black box). The graded ladder (PSA 10 /
BGS 9.5 / …) is rolled up from the same sold-comps data so the card detail needs
a single round-trip.
"""

from __future__ import annotations

import asyncio
import statistics
from typing import Any

from app.services.catalog import card_search_service
from app.services.market import grade_summary_service, marketplace_prices_service

# Weights for the raw-card equilibrium — re-normalised over the signals that are
# actually present, so a card with only a catalog price still gets a value.
_W_COMPS = 0.5
_W_LISTINGS = 0.3
_W_CATALOG = 0.2

_UNGRADED = "UNGRADED"


def _amount(money: Any) -> float | None:
    if isinstance(money, dict) and money.get("amount") is not None:
        try:
            v = float(money["amount"])
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _catalog_signal(card: dict[str, Any]) -> float | None:
    pricing = card.get("pricing_summary") or {}
    return (
        _amount(pricing.get("market"))
        or _amount(pricing.get("low"))
        or _amount(pricing.get("mid"))
    )


def _listings_signal(marketplace: dict[str, Any] | None) -> float | None:
    # Ask-side prices only — PriceCharting is a sold-price guide and is used as
    # the comps signal instead, so exclude it here to avoid double-counting.
    prices = [
        a
        for row in (marketplace or {}).get("providers", [])
        if "pricecharting"
        not in f"{row.get('source') or ''} {row.get('label') or ''}".lower()
        and (a := _amount(row.get("price"))) is not None
    ]
    return statistics.median(prices) if prices else None


def _ungraded_comp_signal(grade_summary: dict[str, Any] | None) -> float | None:
    for row in (grade_summary or {}).get("grades", []):
        if row.get("grade") == _UNGRADED:
            median = row.get("median_recent")
            if isinstance(median, (int, float)) and median > 0:
                return float(median)
            return _amount(row.get("last_sale"))
    return None


def _pricecharting_signal(marketplace: dict[str, Any] | None) -> float | None:
    """PriceCharting's price is a recent-sales guide — a real "sold" signal."""
    for row in (marketplace or {}).get("providers", []):
        src = f"{row.get('source') or ''} {row.get('label') or ''}".lower()
        if "pricecharting" in src:
            return _amount(row.get("price"))
    return None


def _money(amount: float | None, currency: str) -> dict[str, Any] | None:
    if amount is None:
        return None
    return {"amount": round(float(amount), 2), "currency": currency}


async def get_valuation(card_id: str) -> dict[str, Any] | None:
    """Equilibrium fair value + transparent signals + the per-grade ladder."""
    card = await card_search_service.get_card(card_id)
    if card is None:
        return None

    marketplace, grade_summary = await asyncio.gather(
        marketplace_prices_service.get_marketplace_prices_for_card(card_id),
        grade_summary_service.get_grade_summary_for_card(card_id),
    )

    catalog = _catalog_signal(card)
    listings = _listings_signal(marketplace)
    # PriceCharting tracks realised sale prices, so prefer it as the "sold
    # comps" signal; fall back to ungraded sold comps when it's unavailable.
    comps = _pricecharting_signal(marketplace) or _ungraded_comp_signal(grade_summary)

    weighted = [
        (comps, _W_COMPS),
        (listings, _W_LISTINGS),
        (catalog, _W_CATALOG),
    ]
    present = [(v, w) for v, w in weighted if v is not None]
    fair_value: float | None = None
    if present:
        total_weight = sum(w for _, w in present)
        fair_value = sum(v * w for v, w in present) / total_weight

    currency = ((card.get("pricing_summary") or {}).get("market") or {}).get(
        "currency"
    ) or "USD"

    return {
        "card_id": card_id,
        "fair_value": _money(fair_value, currency),
        "confidence": len(present),  # 0-3 signals agreed; more = tighter estimate
        "signals": {
            "sold_comps": _money(comps, currency),
            "listings": _money(listings, currency),
            "catalog": _money(catalog, currency),
        },
        "grades": (grade_summary or {}).get("grades", []),
    }


__all__ = ["get_valuation"]
