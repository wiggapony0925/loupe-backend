"""PokemonPriceTracker provider — config gate + price/graded-comp parsing."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import reload_settings
from app.integrations.base import close_http_client
from app.integrations.pokemonpricetracker import PokemonPriceTrackerProvider


@pytest.fixture(autouse=True)
async def _client_lifecycle():
    yield
    await close_http_client()


@pytest.mark.asyncio
async def test_not_configured_returns_empty(monkeypatch):
    monkeypatch.setenv("POKEMONPRICETRACKER_API_KEY", "")
    reload_settings()
    p = PokemonPriceTrackerProvider()
    assert p.is_configured() is False
    assert await p.get_market_price("charizard") is None
    assert await p.search_sold_comps("charizard") == []


@pytest.mark.asyncio
async def test_market_price_parsed(monkeypatch):
    monkeypatch.setenv("POKEMONPRICETRACKER_API_KEY", "key-1")
    reload_settings()
    p = PokemonPriceTrackerProvider()
    assert p.is_configured() is True

    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://www.pokemonpricetracker.com/api/v2/cards").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Charizard",
                            "id": "base1-4",
                            "prices": {"market": 125.5, "low": 95.0, "high": 150.0},
                        }
                    ]
                },
            )
        )
        out = await p.get_market_price("charizard")
    assert out is not None
    assert out.source == "pokemonpricetracker"
    assert out.market == 125.5
    assert out.low == 95.0
    assert out.high == 150.0


@pytest.mark.asyncio
async def test_graded_comps_parsed(monkeypatch):
    monkeypatch.setenv("POKEMONPRICETRACKER_API_KEY", "key-1")
    reload_settings()
    p = PokemonPriceTrackerProvider()

    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://www.pokemonpricetracker.com/api/v2/cards").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "name": "Charizard",
                            "ebay": {
                                "psa10": {"avg": 450},
                                "psa9": {"avg": 400},
                                "psa8": {"avg": 250},
                            },
                        }
                    ]
                },
            )
        )
        out = await p.search_sold_comps("charizard", days=90, limit=10)

    assert len(out) == 3
    by_grade = {c.grade: c for c in out}
    assert by_grade["10"].price == 450.0
    assert by_grade["10"].house == "psa"
    assert all(c.source == "pokemonpricetracker" for c in out)


@pytest.mark.asyncio
async def test_swallows_500(monkeypatch):
    monkeypatch.setenv("POKEMONPRICETRACKER_API_KEY", "key-1")
    reload_settings()
    p = PokemonPriceTrackerProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(
            url__startswith="https://www.pokemonpricetracker.com/api/v2/cards"
        ).mock(return_value=httpx.Response(500))
        assert await p.get_market_price("charizard") is None
        assert await p.search_sold_comps("charizard") == []
