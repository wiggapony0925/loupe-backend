"""``GET /v1/cards/{id}/listings`` endpoint shape + fan-out."""

from __future__ import annotations

import pytest

from app.integrations import registry as registry_mod
from app.integrations.base import Listing
from app.services import card_search_service, listings_service
from tests.conftest import assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (card_search_service, listings_service):
        monkeypatch.setattr(mod, "_cache_get", _g, raising=False)
        monkeypatch.setattr(mod, "_cache_set", _s, raising=False)


def _patch_card(monkeypatch):
    async def fake(_id):
        return {
            "id": "base1-4",
            "name": "Charizard",
            "number": "4",
            "set": {"name": "Base Set"},
            "tcgplayer": {"prices": {"holofoil": {"market": 500.0}}},
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)


@pytest.mark.asyncio
async def test_listings_endpoint_shape(client, monkeypatch):
    _patch_card(monkeypatch)

    class _Reg:
        async def fan_out_listings(self, query, *, limit):
            return [
                Listing(source="ebay", title=query, price=42.5, url="https://ebay/1")
            ]

    monkeypatch.setattr(registry_mod, "get_registry", lambda: _Reg())
    monkeypatch.setattr(listings_service, "get_registry", lambda: _Reg())

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/listings?limit=5")
    body = assert_envelope_ok(resp)
    assert body["card_id"] == _COMPOSITE
    assert len(body["listings"]) == 1
    row = body["listings"][0]
    assert row["source"] == "ebay"
    assert row["price"] == {"amount": 42.5, "currency": "USD"}


@pytest.mark.asyncio
async def test_listings_invalid_card_id(client):
    # Non-composite + non-UUID strings resolve to no card → 404.
    resp = await client.get("/v1/cards/not-a-composite/listings")
    assert resp.status_code == 404
