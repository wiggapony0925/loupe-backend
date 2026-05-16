"""GoCollect stub provider (population/market). Currently env-gated no-op."""

from __future__ import annotations

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice, PopulationReport


class GoCollectProvider(BaseProvider):
    id = "gocollect"
    name = "GoCollect"

    def is_configured(self) -> bool:
        return bool(get_settings().gocollect_api_key)

    async def get_population(self, spec_or_query: str) -> list[PopulationReport] | None:
        # Stub: returns None until paid API integrated.
        return None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        return None


__all__ = ["GoCollectProvider"]
