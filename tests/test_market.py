"""Tests for ``GET /v1/cards/{id}/market`` synthesizer."""

from __future__ import annotations

from typing import Any

import pytest

from app.services import card_search_service, market_service
from tests.conftest import assert_envelope_ok

_POKEMON_ID = "base1-4"
_COMPOSITE = f"pokemontcg:{_POKEMON_ID}"


def _fake_pokemon_card(market_amt: float | None = 1234.56) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": _POKEMON_ID,
        "name": "Charizard",
        "number": "4",
        "rarity": "Holo Rare",
        "set": {"id": "base1", "name": "Base Set", "releaseDate": "1999/01/09"},
        "images": {"small": "https://img/4.png", "large": "https://img/4l.png"},
    }
    if market_amt is not None:
        card["tcgplayer"] = {
            "url": "https://tcg/4",
            "updatedAt": "2024-01-01",
            "prices": {
                "holofoil": {
                    "market": market_amt,
                    "low": market_amt * 0.6,
                    "mid": market_amt * 0.9,
                    "high": market_amt * 1.3,
                }
            },
        }
    return card


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Disable both the search-service and market-service Redis caches."""

    async def _noop_get(_key: str) -> None:
        return None

    async def _noop_set(*_a, **_kw) -> None:
        return None

    monkeypatch.setattr(card_search_service, "_cache_get", _noop_get)
    monkeypatch.setattr(card_search_service, "_cache_set", _noop_set)
    monkeypatch.setattr(market_service, "_cache_get", _noop_get)
    monkeypatch.setattr(market_service, "_cache_set", _noop_set)


def _patch_upstream(monkeypatch, market_amt: float | None = 1234.56) -> None:
    async def fake_get(card_id: str) -> dict[str, Any]:
        assert card_id == _POKEMON_ID
        return _fake_pokemon_card(market_amt)

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake_get)


@pytest.mark.asyncio
async def test_market_shape(client, monkeypatch):
    _patch_upstream(monkeypatch, market_amt=1000.0)

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/market")
    body = assert_envelope_ok(resp)
    assert body["card_id"] == _COMPOSITE
    snap = body["snapshot"]
    assert set(snap["history"].keys()) == {"30d", "90d", "1y", "all"}

    houses = snap["houses"]
    assert len(houses) == 5
    house_ids = {h["house"] for h in houses}
    assert house_ids == {"psa", "cgc", "bgs", "sgc", "tag"}

    psa = next(h for h in houses if h["house"] == "psa")
    top = psa["grades"][0]
    assert top["house"] == "psa"
    assert top["grade"] == 10
    assert top["population"] > 0
    assert top["market"]["amount"] > 0

    summary = snap["summary"]
    assert summary["raw"]["amount"] == 1000.0
    assert summary["pop_total"] > 0
    assert summary["pop_top"]["amount"] == top["market"]["amount"]
    assert snap["tiers_total"] > 0


@pytest.mark.asyncio
async def test_market_is_deterministic(client, monkeypatch):
    _patch_upstream(monkeypatch, market_amt=750.0)

    a = (await client.get(f"/v1/cards/{_COMPOSITE}/market")).json()["data"]
    b = (await client.get(f"/v1/cards/{_COMPOSITE}/market")).json()["data"]
    # History points carry wall-clock timestamps; the *graded* synthesis must
    # match exactly across calls when seeded by the same card id.
    assert a["snapshot"]["houses"] == b["snapshot"]["houses"]
    assert a["snapshot"]["summary"]["pop_top"] == b["snapshot"]["summary"]["pop_top"]
    assert a["snapshot"]["tiers_total"] == b["snapshot"]["tiers_total"]


@pytest.mark.asyncio
async def test_market_missing_price_is_graceful(client, monkeypatch):
    _patch_upstream(monkeypatch, market_amt=None)

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/market")
    body = assert_envelope_ok(resp)
    snap = body["snapshot"]
    assert snap["summary"]["raw"] is None
    assert snap["summary"]["pop_top"] is None
    assert snap["houses"] == []
    assert snap["tiers_total"] == 0


@pytest.mark.asyncio
async def test_market_requires_composite_id(client):
    # Non-composite + non-UUID strings resolve to no card → 404.
    resp = await client.get("/v1/cards/not-a-composite/market")
    assert resp.status_code == 404
