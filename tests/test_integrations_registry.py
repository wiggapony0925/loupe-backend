"""Registry capability detection + fan-out aggregation."""

from __future__ import annotations

import pytest

from app.integrations import (
    BaseProvider,
    Listing,
    MarketPrice,
    PopulationReport,
    SoldComp,
    get_registry,
)
from app.integrations.registry import ProviderRegistry, reset_registry


class _AlwaysOn(BaseProvider):
    id = "always_on"
    name = "Always-On"

    def is_configured(self) -> bool:
        return True

    async def search_listings(self, query, *, limit=20):
        return [Listing(source=self.id, title=query, price=10.0, url="u")]

    async def search_sold_comps(self, query, *, days=90, limit=50):
        return [
            SoldComp(
                source=self.id, title=query, price=20.0, sold_at="2025-01-01T00:00:00Z"
            )
        ]

    async def get_population(self, spec):
        return [PopulationReport(source=self.id, house="psa", grade="10", population=5)]

    async def get_market_price(self, query):
        return MarketPrice(source=self.id, market=33.0)


class _Off(BaseProvider):
    id = "off"
    name = "Off"

    def is_configured(self) -> bool:
        return False

    async def search_listings(self, query, *, limit=20):
        return [Listing(source=self.id, title="never", price=1.0)]


@pytest.fixture(autouse=True)
def _reset():
    reset_registry()
    yield
    reset_registry()


def test_singleton_get_registry():
    a = get_registry()
    b = get_registry()
    assert a is b
    assert len(a.all) == 6


def test_by_capability_excludes_unconfigured_and_base():
    reg = ProviderRegistry([_AlwaysOn(), _Off()])
    listings = reg.by_capability("listings")
    assert [p.id for p in listings] == ["always_on"]
    pops = reg.by_capability("population")
    assert [p.id for p in pops] == ["always_on"]


def test_status_reports_capabilities():
    reg = ProviderRegistry([_AlwaysOn(), _Off()])
    rows = reg.status()
    by_id = {r["id"]: r for r in rows}
    assert by_id["always_on"]["configured"] is True
    assert set(by_id["always_on"]["capabilities"]) == {
        "listings",
        "comps",
        "population",
        "market_price",
    }
    assert by_id["off"]["configured"] is False
    assert by_id["off"]["capabilities"] == ["listings"]


@pytest.mark.asyncio
async def test_fan_out_listings_merges_and_sorts():
    reg = ProviderRegistry([_AlwaysOn(), _AlwaysOn()])
    out = await reg.fan_out_listings("hello", limit=10)
    assert len(out) == 2
    assert all(x.price == 10.0 for x in out)


@pytest.mark.asyncio
async def test_fan_out_market_price_filters_nones():
    reg = ProviderRegistry([_AlwaysOn()])
    out = await reg.fan_out_market_price("x")
    assert len(out) == 1 and out[0].market == 33.0
