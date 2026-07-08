"""PriceCharting price-field → grade mapping (the reusable core).

PriceCharting reuses its video-game price columns for trading cards; each
column maps to a specific graded market (see the Prices API "Description of
Keys"). This module is the single place that mapping lives, so the per-card
API path (:mod:`.provider`) and the bulk CSV path (:mod:`.csv_sync`) produce
**identical** ladders from the same raw fields — the whole point of keeping the
integration DRY across tiers.
"""

from __future__ import annotations

from typing import Any

#: Ordered low → high grade. Keys are PriceCharting field / CSV-column names;
#: the "10" tiers are house-specific in the API.
CARD_GRADE_LABELS: tuple[tuple[str, str], ...] = (
    ("loose-price", "UNGRADED"),  # raw / ungraded
    ("cib-price", "PSA 7"),  # "Grade 7 or 7.5"
    ("new-price", "PSA 8"),  # "Grade 8 or 8.5"
    ("graded-price", "PSA 9"),  # "Grade 9"
    ("box-only-price", "BGS 9.5"),  # "Grade 9.5" (PSA doesn't issue 9.5)
    ("manual-only-price", "PSA 10"),  # explicitly "Graded 10 by PSA"
    ("bgs-10-price", "BGS 10"),
    ("condition-17-price", "CGC 10"),
    ("condition-18-price", "SGC 10"),
)

#: The graded fields whose presence marks a richer subscription tier (Collector
#: returns only ``loose-price``; higher tiers fill these).
GRADED_FIELDS: tuple[str, ...] = tuple(
    key for key, label in CARD_GRADE_LABELS if label != "UNGRADED"
)


def cents_to_dollars(value: Any) -> float | None:
    """PriceCharting encodes every price as an integer number of pennies.

    Also tolerates CSV strings (``"17244"``, ``"172.44"``, ``""``)."""
    if value is None or value == "":
        return None
    try:
        # CSV values arrive as strings; a dot means it's already dollars.
        if isinstance(value, str) and "." in value:
            return round(float(value), 2)
        return round(int(value) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def card_grade_ladder(data: dict[str, Any]) -> dict[str, float]:
    """The full per-grade price ladder present in a product row
    (``grade label → USD``). Absent / zero grades are omitted, so a token whose
    tier only returns the raw price yields ``{"UNGRADED": …}`` and richer tiers
    light up the rest automatically — no code change on upgrade."""
    ladder: dict[str, float] = {}
    for key, label in CARD_GRADE_LABELS:
        price = cents_to_dollars(data.get(key))
        if price is not None and price > 0:
            ladder[label] = price
    return ladder


def has_graded_fields(data: dict[str, Any]) -> bool:
    """True when a product row carries any real graded price — the signal a
    subscription tier exposes more than the raw price."""
    return any(cents_to_dollars(data.get(key)) for key in GRADED_FIELDS)


__all__ = [
    "CARD_GRADE_LABELS",
    "GRADED_FIELDS",
    "card_grade_ladder",
    "cents_to_dollars",
    "has_graded_fields",
    "int_or_none",
]
