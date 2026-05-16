"""eBay provider — token + listings + comps parsing (mocked via respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import reload_settings
from app.integrations.base import close_http_client
from app.integrations.ebay import EbayProvider


@pytest.fixture(autouse=True)
async def _client_lifecycle():
    yield
    await close_http_client()


@pytest.mark.asyncio
async def test_ebay_not_configured_returns_empty(monkeypatch):
    monkeypatch.delenv("EBAY_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    monkeypatch.delenv("EBAY_CERT_ID", raising=False)
    reload_settings()
    p = EbayProvider()
    assert p.is_configured() is False
    assert await p.search_listings("charizard") == []
    assert await p.search_sold_comps("charizard") == []


@pytest.mark.asyncio
async def test_ebay_listings_parsed(monkeypatch):
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", "tok-123")
    reload_settings()
    p = EbayProvider()
    assert p.is_configured() is True

    with respx.mock(assert_all_called=False) as router:
        router.get(
            url__startswith="https://api.ebay.com/buy/browse/v1/item_summary/search"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "itemSummaries": [
                        {
                            "title": "PSA 10 Charizard Base Set",
                            "price": {"value": "199.99", "currency": "USD"},
                            "itemWebUrl": "https://ebay/x",
                            "buyingOptions": ["FIXED_PRICE"],
                            "image": {"imageUrl": "https://img/x.jpg"},
                            "condition": "Used",
                        }
                    ]
                },
            )
        )
        out = await p.search_listings("charizard", limit=5)
    assert len(out) == 1
    assert out[0].source == "ebay"
    assert out[0].price == 199.99
    assert out[0].is_auction is False


@pytest.mark.asyncio
async def test_ebay_listings_swallows_500(monkeypatch):
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", "tok-123")
    reload_settings()
    p = EbayProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.ebay.com/buy/browse").mock(
            return_value=httpx.Response(500)
        )
        out = await p.search_listings("charizard")
    assert out == []


@pytest.mark.asyncio
async def test_ebay_sold_comps_parse_grade(monkeypatch):
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", "tok-123")
    reload_settings()
    p = EbayProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(
            url__startswith="https://api.ebay.com/buy/marketplace_insights"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "itemSales": [
                        {
                            "title": "Charizard PSA 9 Base Set",
                            "lastSoldPrice": {"value": "85.00", "currency": "USD"},
                            "lastSoldDate": "2025-02-01T10:00:00Z",
                            "itemWebUrl": "https://ebay/y",
                        }
                    ]
                },
            )
        )
        out = await p.search_sold_comps("charizard", days=30, limit=10)
    assert len(out) == 1
    assert out[0].house == "psa"
    assert out[0].grade == "9"
    assert out[0].price == 85.0
