"""PriceCharting integration — our primary price source.

Structured so each concern lives on its own and the whole thing adapts to the
active subscription tier automatically:

* :mod:`.grades`   — the field → grade-ladder mapping (shared by every path).
* :mod:`.provider` — per-card live API (Collector / premium tiers).
* :mod:`.csv_sync` — Legendary bulk price-guide mirror (pre-built, auto-active).
* :mod:`.sealed`   — sealed-product SKU resolution.
* :mod:`.tiers`    — capability detection → tier + fallback strategy.

Public names are re-exported here so existing imports
(``from app.integrations.pricecharting import PriceChartingProvider`` /
``resolve_sealed_market``) keep working after the split.
"""

from __future__ import annotations

from app.integrations.pricecharting import (
    csv_sync,
    grades,
    tiers,
)
from app.integrations.pricecharting.provider import (
    PriceChartingProvider,
    reduce_product,
)
from app.integrations.pricecharting.sealed import resolve_sealed_market

__all__ = [
    "PriceChartingProvider",
    "csv_sync",
    "grades",
    "reduce_product",
    "resolve_sealed_market",
    "tiers",
]
