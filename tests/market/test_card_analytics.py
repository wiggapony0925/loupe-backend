"""Tests for ``GET /v1/cards/{id}/analytics`` — derived market metrics."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.market import (
    card_analytics_service,
    market_service,
    sold_comps_service,
)
from tests.conftest import assert_envelope_ok

_POKEMON_ID = "base1-4"
_COMPOSITE = f"pokemontcg:{_POKEMON_ID}"


def _fake_card(market_amt: float | None = 1000.0) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": _POKEMON_ID,
        "name": "Charizard",
        "number": "4",
        "set": {"id": "base1", "name": "Base Set", "releaseDate": "1999/01/09"},
        "images": {"small": "https://img/4.png", "large": "https://img/4l.png"},
    }
    if market_amt is not None:
        card["tcgplayer"] = {
            "url": "https://tcg/4",
            "updatedAt": "2024-01-01",
            "prices": {"holofoil": {"market": market_amt}},
        }
    return card


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_key: str) -> None:
        return None

    async def _s(*_a, **_kw) -> None:
        return None

    for mod in (card_search_service, market_service, sold_comps_service):
        monkeypatch.setattr(mod, "_cache_get", _g, raising=False)
        monkeypatch.setattr(mod, "_cache_set", _s, raising=False)


def _patch(
    monkeypatch,
    market_amt: float | None = 1000.0,
    comps: list[dict[str, Any]] | None = None,
) -> None:
    async def fake_get(card_id: str) -> dict[str, Any]:
        assert card_id == _POKEMON_ID
        return _fake_card(market_amt)

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)

    async def fake_comps(card_id: str, **_kw) -> dict[str, Any]:
        return {"card_id": card_id, "comps": comps or []}

    monkeypatch.setattr(sold_comps_service, "get_comps_for_card", fake_comps)


@pytest.mark.asyncio
async def test_analytics_composes_metrics(client, monkeypatch):
    _patch(monkeypatch, market_amt=1000.0, comps=[{"x": 1}, {"x": 2}, {"x": 3}])

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/analytics")
    body = assert_envelope_ok(resp)

    assert body["card_id"] == _COMPOSITE
    assert float(body["market_price_usd"]) == 1000.0
    assert body["population"] > 0
    # Market cap = graded_avg x population -> positive.
    assert float(body["market_cap_usd"]) > 0
    # Grade premium = top-grade / raw -> a multiple above 1 for a vintage card.
    assert body["grade_premium"] > 1
    # Momentum windows are always present (value may be null when flat).
    for k in ("momentum_7d", "momentum_30d", "momentum_90d", "momentum_1y"):
        assert k in body
    # Extremes derived from the all-time history.
    assert body["all_time_high_usd"] is not None
    assert body["all_time_low_usd"] is not None
    # Liquidity is the count of comps in the trailing window.
    assert body["liquidity_30d"] == 3


@pytest.mark.asyncio
async def test_analytics_is_deterministic(client, monkeypatch):
    _patch(monkeypatch, market_amt=750.0)

    a = (await client.get(f"/v1/cards/{_COMPOSITE}/analytics")).json()["data"]
    b = (await client.get(f"/v1/cards/{_COMPOSITE}/analytics")).json()["data"]
    assert a["market_cap_usd"] == b["market_cap_usd"]
    assert a["grade_premium"] == b["grade_premium"]
    assert a["population"] == b["population"]


@pytest.mark.asyncio
async def test_analytics_missing_price_is_graceful(client, monkeypatch):
    _patch(monkeypatch, market_amt=None)

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/analytics")
    body = assert_envelope_ok(resp)
    assert body["market_price_usd"] is None
    assert body["population"] == 0
    assert body["market_cap_usd"] is None
    assert body["grade_premium"] is None


@pytest.mark.asyncio
async def test_analytics_unknown_card_404(client):
    resp = await client.get("/v1/cards/not-a-composite/analytics")
    assert resp.status_code == 404


def test_volatility_helper():
    # Flat series -> 0% CoV; varied series -> positive; <2 points -> None.
    assert card_analytics_service._volatility([{"price": 10}, {"price": 10}]) == 0.0
    assert (
        card_analytics_service._volatility(
            [{"price": 10}, {"price": 20}, {"price": 30}]
        )
        > 0
    )
    assert card_analytics_service._volatility([{"price": 10}]) is None
