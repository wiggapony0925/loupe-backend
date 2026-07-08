"""PriceCharting API provider — per-card graded + raw market prices.

This is the *per-request* price path (used at the Collector / premium API
tiers). At the Legendary tier the bulk CSV mirror (:mod:`.csv_sync`) fronts it,
but this always remains the fallback, so the app keeps working on any tier.

API docs: https://www.pricecharting.com/api-documentation
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice
from app.integrations.pricecharting import csv_sync, grades
from app.utils.logger import get_logger

logger = get_logger("integrations.pricecharting")

BASE_URL = "https://www.pricecharting.com/api"


def token() -> str | None:
    s = get_settings()
    return s.pricecharting_token or s.pricecharting_api_key or None


class PriceChartingProvider(BaseProvider):
    id = "pricecharting"
    name = "PriceCharting"

    def is_configured(self) -> bool:
        return token() is not None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        # Mirror-first: at the Legendary tier the whole price guide is mirrored
        # locally, so serve from there (zero API cost, no rate limit). The
        # mirror is only consulted when a bulk sync has actually populated it;
        # otherwise this is a no-op and we hit the API.
        mirrored = await csv_sync.lookup_market_price(query)
        if mirrored is not None:
            return mirrored
        url = f"{BASE_URL}/product?t={token()}&q={quote(query)}"
        try:
            resp = await self._call_with_retry("GET", url)
            if resp is None or resp.status_code >= 400:
                return None
            return reduce_product(resp.json() or {})
        except Exception as exc:
            logger.warning("pricecharting get_market_price failed: %s", exc)
            return None


def reduce_product(data: dict[str, Any]) -> MarketPrice | None:
    """Convert a PriceCharting product row (API JSON or CSV dict) to a
    ``MarketPrice`` — keeping the FULL grade ladder + metadata in extras."""
    loose = grades.cents_to_dollars(data.get("loose-price"))
    new = grades.cents_to_dollars(data.get("new-price"))
    graded = grades.cents_to_dollars(
        data.get("graded-price")
    ) or grades.cents_to_dollars(data.get("manual-only-price"))
    ladder = grades.card_grade_ladder(data)
    if not any((loose, new, graded)) and not ladder:
        return None
    # Keep the low/mid/high/market shape stable (valuation depends on it) and
    # carry the rest of the response — the full grade ladder, yearly sales
    # volume, PriceCharting id, release date — in extras.
    extras: dict[str, Any] = {
        "product_name": data.get("product-name"),
        "console": data.get("console-name"),
    }
    if ladder:
        extras["grade_ladder"] = ladder
    sales_volume = grades.int_or_none(data.get("sales-volume"))
    if sales_volume is not None:
        extras["sales_volume"] = sales_volume
    if data.get("id"):
        extras["pc_id"] = str(data.get("id"))
    if data.get("release-date"):
        extras["release_date"] = data.get("release-date")
    return MarketPrice(
        source="pricecharting",
        market=graded or loose or new,
        low=loose,
        mid=new,
        high=graded,
        extras=extras,
    )


__all__ = ["BASE_URL", "PriceChartingProvider", "reduce_product", "token"]
