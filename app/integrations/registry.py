"""Provider registry — discovery, capability routing, and fan-out helpers."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from app.integrations.base import (
    BaseProvider,
    Listing,
    MarketPrice,
    PopulationReport,
    SoldComp,
)
from app.integrations.ebay import EbayProvider
from app.integrations.gocollect import GoCollectProvider
from app.integrations.justtcg import JustTcgProvider
from app.integrations.point130 import Point130Provider
from app.integrations.pokemon_tcg import PokemonTcgProvider
from app.integrations.pricecharting import PriceChartingProvider
from app.integrations.psa import PsaProvider
from app.integrations.tcgcsv import TcgCsvProvider
from app.integrations.tcgplayer import TcgPlayerProvider
from app.utils.logger import get_logger

logger = get_logger("integrations.registry")

_FANOUT_TIMEOUT_S = 4.0


class ProviderRegistry:
    """Holds singleton provider instances and dispatches capability calls."""

    def __init__(self, providers: list[BaseProvider]):
        self._providers: list[BaseProvider] = providers

    @property
    def all(self) -> list[BaseProvider]:
        return list(self._providers)

    def get(self, provider_id: str) -> BaseProvider | None:
        return next((p for p in self._providers if p.id == provider_id), None)

    def by_capability(self, capability: str) -> list[BaseProvider]:
        """Providers whose override of *capability* is callable AND configured."""
        method_name = {
            "listings": "search_listings",
            "comps": "search_sold_comps",
            "population": "get_population",
            "market_price": "get_market_price",
        }.get(capability, capability)
        out: list[BaseProvider] = []
        for p in self._providers:
            if not p.is_configured():
                continue
            base_fn = getattr(BaseProvider, method_name, None)
            own_fn = getattr(type(p), method_name, None)
            if base_fn is None or own_fn is None:
                continue
            if own_fn is base_fn:
                continue
            out.append(p)
        return out

    def status(self) -> list[dict[str, Any]]:
        """Per-provider status used by ``/v1/providers/status``."""
        out: list[dict[str, Any]] = []
        for p in self._providers:
            configured = p.is_configured()
            caps: list[str] = []
            for cap, method in (
                ("listings", "search_listings"),
                ("comps", "search_sold_comps"),
                ("population", "get_population"),
                ("market_price", "get_market_price"),
            ):
                base_fn = getattr(BaseProvider, method)
                own_fn = getattr(type(p), method, base_fn)
                if own_fn is not base_fn:
                    caps.append(cap)
            out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "configured": configured,
                    "capabilities": caps,
                }
            )
        return out

    # ---- fan-out helpers ----------------------------------------------

    async def fan_out_listings(self, query: str, *, limit: int = 20) -> list[Listing]:
        providers = self.by_capability("listings")
        if not providers:
            return []
        results = await self._gather(
            [p.search_listings(query, limit=limit) for p in providers]
        )
        merged: list[Listing] = []
        for r in results:
            if isinstance(r, list):
                merged.extend(r)
        merged.sort(key=lambda x: x.price)
        return merged[:limit]

    async def fan_out_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        providers = self.by_capability("comps")
        if not providers:
            return []
        results = await self._gather(
            [p.search_sold_comps(query, days=days, limit=limit) for p in providers]
        )
        merged: list[SoldComp] = []
        for r in results:
            if isinstance(r, list):
                merged.extend(r)
        merged.sort(key=lambda x: x.sold_at, reverse=True)
        return merged[:limit]

    async def fan_out_population(self, spec_or_query: str) -> list[PopulationReport]:
        providers = self.by_capability("population")
        if not providers:
            return []
        results = await self._gather(
            [p.get_population(spec_or_query) for p in providers]
        )
        out: list[PopulationReport] = []
        for r in results:
            if isinstance(r, list):
                out.extend(r)
        return out

    async def fan_out_market_price(self, query: str) -> list[MarketPrice]:
        providers = self.by_capability("market_price")
        if not providers:
            return []
        results = await self._gather([p.get_market_price(query) for p in providers])
        out: list[MarketPrice] = []
        for r in results:
            if isinstance(r, MarketPrice):
                out.append(r)
        return out

    @staticmethod
    async def _gather(coros: list[Any]) -> list[Any]:
        async def safe(coro: Any) -> Any:
            try:
                return await asyncio.wait_for(coro, timeout=_FANOUT_TIMEOUT_S)
            except Exception as exc:
                # Per-provider fan-out failures used to vanish at DEBUG; now
                # they're reported with the provider tagged so dashboards can
                # show "PSA error rate" without log spelunking.
                from app.platform.observability import capture_integration_error

                provider_name = getattr(
                    getattr(coro, "cr_frame", None), "f_locals", {}
                ).get("self", None)
                capture_integration_error(
                    exc,
                    integration=type(provider_name).__name__
                    if provider_name is not None
                    else "unknown",
                    operation="fan_out",
                )
                return None

        return await asyncio.gather(*(safe(c) for c in coros))


@lru_cache(maxsize=1)
def get_registry() -> ProviderRegistry:
    return ProviderRegistry(
        providers=[
            EbayProvider(),
            PsaProvider(),
            TcgPlayerProvider(),
            PriceChartingProvider(),
            Point130Provider(),
            GoCollectProvider(),
            # Free / low-friction fallbacks — keep last so paid/official
            # providers take priority in fan-out aggregations.
            PokemonTcgProvider(),
            TcgCsvProvider(),
            JustTcgProvider(),
        ]
    )


def reset_registry() -> None:
    """Test helper: clear the cached singleton."""
    get_registry.cache_clear()


__all__ = ["ProviderRegistry", "get_registry", "reset_registry"]
