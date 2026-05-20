"""JustTCG provider — aggregated TCG market prices (env-gated stub).

API docs: https://justtcg.com/api — free tier ~1k requests/day.
Drop in ``JUSTTCG_API_KEY`` and this provider activates automatically.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice
from app.utils.logger import get_logger

logger = get_logger("integrations.justtcg")

_BASE = "https://api.justtcg.com/v1"


class JustTcgProvider(BaseProvider):
    id = "justtcg"
    name = "JustTCG"

    def is_configured(self) -> bool:
        return bool(getattr(get_settings(), "justtcg_api_key", None))

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        key = get_settings().justtcg_api_key
        url = f"{_BASE}/cards?q={quote(query)}&limit=1"
        try:
            resp = await self._call_with_retry(
                "GET", url, headers={"X-API-Key": key or ""}
            )
            if resp is None or resp.status_code >= 400:
                return None
            data = resp.json() or {}
        except Exception as exc:
            logger.warning("justtcg get_market_price failed: %s", exc)
            return None
        return self._reduce(data)

    @staticmethod
    def _reduce(data: dict[str, Any]) -> MarketPrice | None:
        items = data.get("data") or data.get("cards") or []
        if not items:
            return None
        card = items[0] if isinstance(items, list) else items
        prices = card.get("prices") or card.get("price") or {}
        if not isinstance(prices, dict):
            return None
        market = _f(prices.get("market") or prices.get("marketPrice"))
        low = _f(prices.get("low") or prices.get("lowPrice"))
        mid = _f(prices.get("mid") or prices.get("midPrice"))
        high = _f(prices.get("high") or prices.get("highPrice"))
        if not any((market, low, mid, high)):
            return None
        return MarketPrice(
            source="justtcg",
            market=market,
            low=low,
            mid=mid,
            high=high,
            extras={"card_name": card.get("name"), "card_id": card.get("id")},
        )


def _f(v: Any) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["JustTcgProvider"]
