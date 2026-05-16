"""PriceCharting provider — graded + raw market prices.

API docs: https://www.pricecharting.com/api-documentation
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice
from app.utils.logger import get_logger

logger = get_logger("integrations.pricecharting")

_BASE = "https://www.pricecharting.com/api"


def _token() -> str | None:
    s = get_settings()
    return s.pricecharting_token or s.pricecharting_api_key or None


class PriceChartingProvider(BaseProvider):
    id = "pricecharting"
    name = "PriceCharting"

    def is_configured(self) -> bool:
        return _token() is not None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        tok = _token()
        url = f"{_BASE}/product?t={tok}&q={quote(query)}"
        try:
            resp = await self._call_with_retry("GET", url)
            if resp is None or resp.status_code >= 400:
                return None
            data = resp.json() or {}
            return self._reduce(data)
        except Exception as exc:
            logger.warning("pricecharting get_market_price failed: %s", exc)
            return None

    @staticmethod
    def _reduce(data: dict[str, Any]) -> MarketPrice | None:
        # PriceCharting returns prices in cents.
        def cents(key: str) -> float | None:
            v = data.get(key)
            try:
                return round(int(v) / 100.0, 2) if v is not None else None
            except (TypeError, ValueError):
                return None

        loose = cents("loose-price")
        new = cents("new-price")
        graded = cents("graded-price") or cents("manual-only-price")
        if not any((loose, new, graded)):
            return None
        return MarketPrice(
            source="pricecharting",
            market=graded or loose or new,
            low=loose,
            mid=new,
            high=graded,
            extras={
                "product_name": data.get("product-name"),
                "console": data.get("console-name"),
            },
        )


__all__ = ["PriceChartingProvider"]
