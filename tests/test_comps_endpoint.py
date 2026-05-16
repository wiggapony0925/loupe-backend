"""``GET /v1/cards/{id}/comps`` endpoint — post-filter + shape."""

from __future__ import annotations

import pytest

from app.integrations.base import SoldComp
from app.services import card_search_service, comps_service
from tests.conftest import assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (card_search_service, comps_service):
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


def _patch_registry(monkeypatch, comps):
    class _Reg:
        async def fan_out_comps(self, query, *, days, limit):
            return comps

    monkeypatch.setattr(comps_service, "get_registry", lambda: _Reg())


@pytest.mark.asyncio
async def test_comps_endpoint_shape(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_registry(
        monkeypatch,
        [
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2400.0,
                sold_at="2025-02-01T00:00:00Z",
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 9",
                price=800.0,
                sold_at="2025-01-15T00:00:00Z",
                grade="9",
                house="psa",
            ),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/comps?days=30")
    body = assert_envelope_ok(resp)
    assert len(body["comps"]) == 2
    assert body["days"] == 30


@pytest.mark.asyncio
async def test_comps_grade_filter(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_registry(
        monkeypatch,
        [
            SoldComp(
                source="ebay",
                title="x",
                price=1.0,
                sold_at="2025-02-01T00:00:00Z",
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="y",
                price=2.0,
                sold_at="2025-02-01T00:00:00Z",
                grade="9",
                house="cgc",
            ),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/comps?grade=10&house=psa")
    body = assert_envelope_ok(resp)
    assert [(c["grade"], c["house"]) for c in body["comps"]] == [("10", "psa")]
