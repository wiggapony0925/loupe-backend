"""Per-grade sales summary for a card.

Powers the "price-by-grade" pill row on the card-detail sheet
(PriceCharting/Collectr style). For each grade observed in the last
``window_days * 2`` of comps we expose:

* ``last_sale`` — most recent sold price
* ``sales_count`` — count in the recent window
* ``delta_amount`` / ``delta_pct`` — median(recent) vs median(previous)

Built on top of :mod:`app.services.market.sold_comps_service` so the
provider fan-out + cache are reused. Ungraded rows are bucketed under
``grade="UNGRADED"`` so the UI can always render a baseline pill.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.market import sold_comps_service

_UNGRADED = "UNGRADED"


def _parse_sold_at(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Accept both "...Z" and "+00:00" forms.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _grade_key(row: dict[str, Any]) -> str:
    grade = row.get("grade")
    house = row.get("house")
    if not grade:
        return _UNGRADED
    g = str(grade).strip().upper()
    if house:
        return f"{str(house).strip().upper()} {g}"
    return g


async def get_grade_summary_for_card(
    card_id: str,
    *,
    window_days: int = 30,
    lookback_days: int = 90,
) -> dict[str, Any] | None:
    """Return a per-grade roll-up of recent comps.

    ``lookback_days`` controls how far back we ask the comps service to
    pull rows; we always also pull the *previous* equal window so the
    30-day delta has a baseline.
    """
    total_days = max(window_days * 2, lookback_days)
    raw = await sold_comps_service.get_comps_for_card(
        card_id, days=total_days, limit=200
    )
    if raw is None:
        return None

    now = datetime.now(UTC)
    cutoff_recent = now - timedelta(days=window_days)
    cutoff_prior = now - timedelta(days=window_days * 2)

    buckets: dict[str, dict[str, Any]] = {}
    for row in raw.get("comps") or []:
        sold_at = _parse_sold_at(row.get("sold_at"))
        if sold_at is None:
            continue
        price = row.get("price") or {}
        amount = price.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        key = _grade_key(row)
        bucket = buckets.setdefault(
            key,
            {
                "grade": key,
                "house": (row.get("house") or "").lower() or None,
                "currency": price.get("currency") or "USD",
                "_recent": [],
                "_prior": [],
                "_last_sale_at": None,
                "_last_sale_price": None,
                "_last_sale_url": None,
            },
        )
        if sold_at >= cutoff_recent:
            bucket["_recent"].append((sold_at, float(amount)))
        elif sold_at >= cutoff_prior:
            bucket["_prior"].append((sold_at, float(amount)))
        if bucket["_last_sale_at"] is None or sold_at > bucket["_last_sale_at"]:
            bucket["_last_sale_at"] = sold_at
            bucket["_last_sale_price"] = float(amount)
            bucket["_last_sale_url"] = row.get("url")

    rows: list[dict[str, Any]] = []
    for key, b in buckets.items():
        recent_prices = [p for _, p in b["_recent"]]
        prior_prices = [p for _, p in b["_prior"]]
        median_recent = statistics.median(recent_prices) if recent_prices else None
        median_prior = statistics.median(prior_prices) if prior_prices else None
        delta_amount: float | None = None
        delta_pct: float | None = None
        if median_recent is not None and median_prior:
            delta_amount = round(median_recent - median_prior, 2)
            delta_pct = round((median_recent - median_prior) / median_prior * 100.0, 2)
        rows.append(
            {
                "grade": key,
                "house": b["house"],
                "currency": b["currency"],
                "last_sale": (
                    {
                        "amount": round(b["_last_sale_price"], 2),
                        "currency": b["currency"],
                        "sold_at": b["_last_sale_at"].isoformat(),
                        "url": b["_last_sale_url"],
                    }
                    if b["_last_sale_price"] is not None
                    else None
                ),
                "median_recent": (
                    round(median_recent, 2) if median_recent is not None else None
                ),
                "sales_count": len(recent_prices),
                "delta_amount": delta_amount,
                "delta_pct": delta_pct,
            }
        )

    # Sort: UNGRADED first (baseline), then by last_sale desc so the
    # most valuable grade is next to it.
    def _sort_key(r: dict[str, Any]) -> tuple[int, float]:
        last = r.get("last_sale") or {}
        amount = last.get("amount") or 0.0
        is_ungraded = r["grade"] == _UNGRADED
        return (0 if is_ungraded else 1, -float(amount))

    rows.sort(key=_sort_key)

    return {
        "card_id": card_id,
        "window_days": window_days,
        "grades": rows,
    }


__all__ = ["get_grade_summary_for_card"]
