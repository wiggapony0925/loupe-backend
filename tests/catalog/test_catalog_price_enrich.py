"""Tests for catalog price enrichment (One Piece / Digimon browse tiles)."""

from __future__ import annotations

import asyncio

import pytest

from app.platform.redis_client import close_redis
from app.services.catalog import card_search_service as css


async def _reset() -> None:
    for t in list(css._price_bg_tasks):
        t.cancel()
    if css._price_bg_tasks:
        await asyncio.gather(*list(css._price_bg_tasks), return_exceptions=True)
    css._price_bg_tasks.clear()
    await close_redis()


@pytest.fixture(autouse=True)
async def _fresh_redis():
    await _reset()
    yield
    await _reset()


@pytest.mark.asyncio
async def test_enrich_attaches_and_caches_prices(monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_chain(name, set_name, card_id):
        calls["n"] += 1
        return {
            "market": {"amount": 4.20, "currency": "USD"},
            "sources": ["pricecharting"],
        }

    monkeypatch.setattr(css, "_pricing_from_market_chain", fake_chain)

    cards = [{"id": "digimoncard:BT1-001", "name": "Agumon"}]
    out = await css.enrich_catalog_prices(cards)
    assert out[0]["pricing_summary"]["market"]["amount"] == 4.20
    assert calls["n"] == 1

    # A second page with the same card is served from cache — no new lookup.
    cards2 = [{"id": "digimoncard:BT1-001", "name": "Agumon"}]
    await css.enrich_catalog_prices(cards2)
    assert cards2[0]["pricing_summary"]["market"]["amount"] == 4.20
    assert calls["n"] == 1  # cached, not re-resolved


@pytest.mark.asyncio
async def test_enrich_skips_already_priced(monkeypatch) -> None:
    async def boom(*a, **k):  # must not be called
        raise AssertionError("should not resolve an already-priced card")

    monkeypatch.setattr(css, "_pricing_from_market_chain", boom)
    cards = [{"id": "x:1", "name": "N", "pricing_summary": {"market": {"amount": 1}}}]
    out = await css.enrich_catalog_prices(cards)
    assert out[0]["pricing_summary"]["market"]["amount"] == 1


@pytest.mark.asyncio
async def test_enrich_caches_negative_result(monkeypatch) -> None:
    calls = {"n": 0}

    async def no_price(name, set_name, card_id):
        calls["n"] += 1
        return

    monkeypatch.setattr(css, "_pricing_from_market_chain", no_price)
    cards = [{"id": "op:1", "name": "Weird Effect Name"}]
    await css.enrich_catalog_prices(cards)
    # A repeat view must NOT re-resolve a card we already know has no price.
    await css.enrich_catalog_prices([{"id": "op:1", "name": "Weird Effect Name"}])
    assert calls["n"] == 1
