"""``/v1/cards/{id}/grade-summary`` and ``/marketplace-prices`` endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.base import Listing, SoldComp
from app.services.catalog import card_search_service
from app.services.market import (
    listings_service,
)
from app.services.market import (
    sold_comps_service as comps_service,
)
from tests.conftest import assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (card_search_service, comps_service, listings_service):
        monkeypatch.setattr(mod, "_cache_get", _g, raising=False)
        monkeypatch.setattr(mod, "_cache_set", _s, raising=False)


def _patch_card(monkeypatch):
    async def fake(_id):
        return {
            "id": "base1-4",
            "name": "Charizard",
            "number": "4",
            "set": {"name": "Base Set"},
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)


def _patch_comps(monkeypatch, comps):
    class _Reg:
        async def fan_out_comps(self, query, *, days, limit):
            return comps

    monkeypatch.setattr(comps_service, "get_registry", lambda: _Reg())


def _patch_listings(monkeypatch, listings):
    class _Reg:
        async def fan_out_listings(self, query, *, limit):
            return listings

    monkeypatch.setattr(listings_service, "get_registry", lambda: _Reg())


# ----------------------------------------------------------- grade-summary


@pytest.mark.asyncio
async def test_grade_summary_pivots_by_grade(client, monkeypatch):
    _patch_card(monkeypatch)
    now = datetime.now(UTC)
    _patch_comps(
        monkeypatch,
        [
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2500.0,
                sold_at=_iso(now - timedelta(days=2)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2400.0,
                sold_at=_iso(now - timedelta(days=10)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2000.0,
                sold_at=_iso(now - timedelta(days=45)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 9",
                price=800.0,
                sold_at=_iso(now - timedelta(days=5)),
                grade="9",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard raw",
                price=300.0,
                sold_at=_iso(now - timedelta(days=3)),
                grade=None,
                house=None,
            ),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    body = assert_envelope_ok(resp)
    assert body["card_id"] == _COMPOSITE
    assert body["window_days"] == 30
    grades = body["grades"]
    # UNGRADED is pinned first.
    assert grades[0]["grade"] == "UNGRADED"
    keys = {g["grade"] for g in grades}
    assert {"UNGRADED", "PSA 10", "PSA 9"} <= keys
    psa10 = next(g for g in grades if g["grade"] == "PSA 10")
    assert psa10["sales_count"] == 2
    assert psa10["last_sale"]["amount"] == 2500.0
    # median(2500, 2400)=2450 vs baseline 2000 → +22.5%.
    assert psa10["delta_pct"] == 22.5


@pytest.mark.asyncio
async def test_grade_summary_empty_comps(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_comps(monkeypatch, [])
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    body = assert_envelope_ok(resp)
    assert body["grades"] == []


@pytest.mark.asyncio
async def test_grade_summary_404_on_unknown_card(client, monkeypatch):
    async def fake(_id):
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    assert resp.status_code == 404


# ------------------------------------------------------ marketplace-prices


@pytest.mark.asyncio
async def test_marketplace_prices_groups_by_source(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_listings(
        monkeypatch,
        [
            Listing(source="ebay", title="A", price=250.0, url="https://ebay/a"),
            Listing(source="ebay", title="B", price=199.0, url="https://ebay/b"),
            Listing(source="tcgplayer", title="C", price=210.0, url="https://tcg/c"),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    body = assert_envelope_ok(resp)
    providers = body["providers"]
    assert len(providers) == 2
    # Sorted by price ascending.
    assert providers[0]["source"] == "ebay"
    assert providers[0]["price"]["amount"] == 199.0
    assert providers[0]["url"] == "https://ebay/b"
    assert providers[0]["label"] == "eBay"
    assert providers[0]["search_url"].startswith("https://www.ebay.com/sch/")
    assert providers[1]["source"] == "tcgplayer"
    assert providers[1]["price"]["amount"] == 210.0


@pytest.mark.asyncio
async def test_marketplace_prices_empty(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_listings(monkeypatch, [])
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    body = assert_envelope_ok(resp)
    assert body["providers"] == []


@pytest.mark.asyncio
async def test_marketplace_prices_404_on_unknown_card(client, monkeypatch):
    async def fake(_id):
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_listings_query_includes_nested_set_name(client, monkeypatch):
    _patch_card(monkeypatch)
    captured: dict[str, str] = {}

    class _Reg:
        async def fan_out_listings(self, query, *, limit):
            captured["query"] = query
            return [Listing(source="ebay", title=query, price=199.0)]

    monkeypatch.setattr(listings_service, "get_registry", lambda: _Reg())

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/listings")
    body = assert_envelope_ok(resp)

    assert captured["query"] == "Charizard Base Set #4"
    assert body["query"] == "Charizard Base Set #4"
