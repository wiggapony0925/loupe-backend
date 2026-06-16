"""eBay provider — token + listings + comps parsing (mocked via respx)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

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
    # Set to "" rather than delenv so empty values override the developer's
    # local `.env` when pydantic-settings re-reads it on reload_settings().
    monkeypatch.setenv("EBAY_OAUTH_TOKEN", "")
    monkeypatch.setenv("EBAY_APP_ID", "")
    monkeypatch.setenv("EBAY_CERT_ID", "")
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
        ).mock(side_effect=lambda request: _listing_response(str(request.url)))
        out = await p.search_listings("charizard", limit=5)
    assert len(out) == 2
    assert out[0].source == "ebay"
    assert out[0].price == 199.99
    assert out[0].is_auction is False
    assert out[1].price == 149.5
    assert out[1].is_auction is True
    assert out[1].time_left_seconds is not None
    assert out[1].time_left_seconds > 0


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


def _listing_response(url: str) -> httpx.Response:
    decoded = unquote(url)
    if "AUCTION" in decoded:
        end_at = (
            (datetime.now(UTC) + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
        )
        return httpx.Response(
            200,
            json={
                "itemSummaries": [
                    {
                        "itemId": "auction-1",
                        "title": "Charizard Base Set auction",
                        "price": {"value": "149.50", "currency": "USD"},
                        "itemWebUrl": "https://ebay/auction",
                        "buyingOptions": ["AUCTION"],
                        "image": {"imageUrl": "https://img/a.jpg"},
                        "condition": "Used",
                        "itemEndDate": end_at,
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "itemSummaries": [
                {
                    "itemId": "fixed-1",
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
