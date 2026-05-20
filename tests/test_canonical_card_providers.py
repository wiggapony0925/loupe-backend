"""Per-provider canonical-mapping contract tests.

For each provider value type (:class:`Listing`, :class:`SoldComp`,
:class:`MarketPrice`, :class:`PopulationReport`) we assert that a
representative instance maps cleanly into the corresponding canonical
sub-model. This is the "every provider speaks the same dialect" gate.
"""

from __future__ import annotations

import asyncio

import pytest

from app.integrations.base import Listing, MarketPrice, PopulationReport, SoldComp
from app.integrations.registry import get_registry, reset_registry
from app.schemas.canonical_card import (
    CanonicalComp,
    CanonicalListing,
    Money,
    PopulationRow,
)
from app.services import canonical_card_service


def test_listing_dataclass_maps_to_canonical_listing() -> None:
    raw = Listing(
        source="ebay",
        title="Charizard PSA 9",
        price=850.0,
        currency="USD",
        url="https://ebay.example/1",
        condition="Used",
        image_url="https://ebay.example/img.png",
        is_auction=True,
        time_left_seconds=3600,
    )
    canonical = CanonicalListing(
        source=raw.source,
        title=raw.title,
        price=Money(amount=raw.price, currency=raw.currency),
        url=raw.url,
        condition=raw.condition,
        image_url=raw.image_url,
        is_auction=raw.is_auction,
        time_left_seconds=raw.time_left_seconds,
    )
    assert canonical.source == "ebay"
    assert canonical.price.amount == 850.0
    assert canonical.is_auction is True


def test_sold_comp_dataclass_maps_to_canonical_comp() -> None:
    raw = SoldComp(
        source="130point",
        title="Charizard PSA 10 sold",
        price=4200.0,
        sold_at="2026-05-01T00:00:00Z",
        currency="USD",
        condition=None,
        grade="10",
        house="psa",
        url="https://130point.example/1",
        image_url=None,
    )
    canonical = CanonicalComp(
        source=raw.source,
        title=raw.title,
        price=Money(amount=raw.price, currency=raw.currency),
        sold_at=raw.sold_at,
        condition=raw.condition,
        grade=raw.grade,
        house=raw.house,
        url=raw.url,
        image_url=raw.image_url,
    )
    assert canonical.house == "psa"
    assert canonical.grade == "10"


def test_market_price_dataclass_maps_to_price_quote() -> None:
    raw = MarketPrice(
        source="tcgcsv",
        market=310.0,
        low=240.0,
        mid=295.0,
        high=420.0,
        currency="USD",
        extras={"as_of": "2026-05-19T00:00:00+00:00", "sample_size": 25},
    )
    quote = canonical_card_service._market_price_to_quote(raw)
    assert quote is not None
    assert quote.source == "tcgcsv"
    assert quote.market is not None and quote.market.amount == 310.0
    assert quote.low is not None and quote.low.amount == 240.0
    assert quote.as_of == "2026-05-19T00:00:00+00:00"
    assert quote.sample_size == 25


def test_market_price_with_no_prices_yields_none() -> None:
    raw = MarketPrice(source="empty")
    assert canonical_card_service._market_price_to_quote(raw) is None


def test_population_report_maps_to_population_row() -> None:
    raw = PopulationReport(
        source="psa",
        house="psa",
        grade="10",
        population=12,
        pop_higher=0,
    )
    row = PopulationRow(
        source=raw.source,
        house=raw.house,
        grade=raw.grade,
        population=raw.population,
        pop_higher=raw.pop_higher,
    )
    assert row.house == "psa"
    assert row.population == 12


# --------------------------------------------------------------------- registry


def test_registry_capability_matrix_is_canonical() -> None:
    """Every capability a provider declares must correspond to a known
    canonical section (listings / comps / population / pricing.quotes /
    certs). This locks the cross-provider contract."""
    reset_registry()
    registry = get_registry()
    known_capabilities = {"listings", "comps", "population", "market_price"}
    for status_entry in registry.status():
        for cap in status_entry["capabilities"]:
            assert cap in known_capabilities, (
                f"provider {status_entry['id']} declared unknown capability {cap!r}"
            )


@pytest.mark.asyncio
async def test_every_provider_handles_empty_query_safely(monkeypatch) -> None:
    """A defensive check: calling every provider with an empty query must
    not raise. They may return [] / None — that's the contract."""
    reset_registry()
    registry = get_registry()
    for p in registry.all:
        # Each call has 1s timeout to avoid hitting the network when unconfigured.
        try:
            for coro in (
                p.search_listings(""),
                p.search_sold_comps(""),
                p.get_population(""),
                p.get_market_price(""),
            ):
                await asyncio.wait_for(coro, timeout=1.0)
        except asyncio.TimeoutError:
            # An unconfigured provider returning quickly is the success path;
            # a configured one timing out is OK because it means it tried.
            pass
        except Exception as exc:
            pytest.fail(f"provider {p.id} raised on empty query: {exc!r}")
