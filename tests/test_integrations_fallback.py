"""All providers off → endpoints return empty real data + market falls back."""

from __future__ import annotations

import pytest

from app.config import reload_settings
from app.integrations.registry import reset_registry
from app.services.catalog import card_search_service
from app.services.market import listings_service, market_service, sold_comps_service as comps_service
from tests.conftest import assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


@pytest.fixture(autouse=True)
def _strip_keys(monkeypatch):
    for k in (
        "EBAY_OAUTH_TOKEN",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
        "PSA_API_TOKEN",
        "TCGPLAYER_PUBLIC_KEY",
        "TCGPLAYER_PRIVATE_KEY",
        "TCGPLAYER_CLIENT_ID",
        "TCGPLAYER_CLIENT_SECRET",
        "PRICECHARTING_TOKEN",
        "PRICECHARTING_API_KEY",
        "GOCOLLECT_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    reload_settings()
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (card_search_service, market_service, listings_service, comps_service):
        monkeypatch.setattr(mod, "_cache_get", _g, raising=False)
        monkeypatch.setattr(mod, "_cache_set", _s, raising=False)


def _fake_card(**_kw):
    return {
        "id": "base1-4",
        "name": "Charizard",
        "number": "4",
        "set": {"id": "base1", "name": "Base Set"},
        "tcgplayer": {
            "prices": {"holofoil": {"market": 500.0}},
        },
    }


def _patch_upstream(monkeypatch):
    async def fake(_id):
        return _fake_card()

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)


@pytest.mark.asyncio
async def test_market_all_synthesized_when_no_providers(client, monkeypatch):
    _patch_upstream(monkeypatch)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/market")
    body = assert_envelope_ok(resp)
    for block in body["snapshot"]["houses"]:
        for row in block["grades"]:
            assert row["source"] == "synthesized"


@pytest.mark.asyncio
async def test_listings_empty_when_no_providers(client, monkeypatch):
    _patch_upstream(monkeypatch)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/listings")
    body = assert_envelope_ok(resp)
    assert body["listings"] == []


@pytest.mark.asyncio
async def test_comps_empty_when_no_providers(client, monkeypatch):
    _patch_upstream(monkeypatch)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/comps")
    body = assert_envelope_ok(resp)
    assert body["comps"] == []
