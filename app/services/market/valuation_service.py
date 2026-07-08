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


def _pricecharting_row(marketplace: dict[str, Any] | None) -> dict[str, Any] | None:
    for row in (marketplace or {}).get("providers", []):
        src = f"{row.get('source') or ''} {row.get('label') or ''}".lower()
        if "pricecharting" in src:
            return row
    return None


def _pricecharting_signal(marketplace: dict[str, Any] | None) -> float | None:
    """PriceCharting's price is a recent-sales guide — a real "sold" signal."""
    row = _pricecharting_row(marketplace)
    return _amount(row.get("price")) if row else None


_GRADE_HOUSES = ("PSA", "BGS", "CGC", "SGC")


def _house_for(grade_label: str) -> str | None:
    upper = grade_label.upper()
    for house in _GRADE_HOUSES:
        if upper.startswith(house):
            return house.lower()
    return None


def _merge_guide_grades(
    comp_grades: list[dict[str, Any]],
    ladder: dict[str, Any] | None,
    currency: str,
) -> list[dict[str, Any]]:
    """Fold PriceCharting's per-grade guide ladder into the sold-comps ladder.

    Real sold comps always win — we only *add* rows for grades comps don't
    cover (which is most, for anything but vintage chase cards), so the
    price-by-grade row is populated even when sales are sparse. Guide rows carry
    ``is_guide=True`` so the client can badge "guide" vs "recent sale"; the
    shape otherwise matches a comp row, so existing clients render them as-is.
    """
    if not ladder:
        return comp_grades
    present = {str(r.get("grade", "")).upper() for r in comp_grades}
    merged = list(comp_grades)
    for label, price in ladder.items():
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        if str(label).upper() in present:
            continue  # a real sale already covers this grade
        merged.append(
            {
                "grade": label,
                "house": _house_for(str(label)),
                "currency": currency,
                "last_sale": None,
                "median_recent": round(float(price), 2),
                "sales_count": 0,
                "delta_amount": None,
                "delta_pct": None,
                "source": "pricecharting",
                "is_guide": True,
            }
        )

    def _sort_key(row: dict[str, Any]) -> tuple[int, float]:
        is_ungraded = str(row.get("grade", "")).upper() == _UNGRADED
        price = row.get("median_recent")
        if not isinstance(price, (int, float)):
            last = row.get("last_sale") or {}
            price = last.get("amount") or 0.0
        return (0 if is_ungraded else 1, -float(price))

    merged.sort(key=_sort_key)
    return merged


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
    pc_row = _pricecharting_row(marketplace)
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

    comp_grades = (grade_summary or {}).get("grades", [])
    ladder = pc_row.get("grade_ladder") if pc_row else None
    grades = _merge_guide_grades(comp_grades, ladder, currency)

    return {
        "card_id": card_id,
        "fair_value": _money(fair_value, currency),
        "confidence": len(present),  # 0-3 signals agreed; more = tighter estimate
        "signals": {
            "sold_comps": _money(comps, currency),
            "listings": _money(listings, currency),
            "catalog": _money(catalog, currency),
        },
        # Yearly units sold from PriceCharting — a real liquidity signal for
        # "how easily does this move".
        "sales_volume": pc_row.get("sales_volume") if pc_row else None,
        "grades": grades,
    }


__all__ = ["get_valuation"]
