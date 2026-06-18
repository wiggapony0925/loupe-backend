"""``GET /v1/cards/{id}/nearby-listings`` endpoint + ApifyProvider mapping."""

from __future__ import annotations

import pytest

from app.integrations.apify import ApifyProvider, _map_item
from app.integrations.base import Listing
from app.services.catalog import card_search_service
from app.services.market import nearby_listings_service
from tests.conftest import assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (card_search_service, nearby_listings_service):
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


@pytest.mark.asyncio
async def test_nearby_listings_endpoint_shape(client, monkeypatch):
    _patch_card(monkeypatch)

    async def fake_search(self, query, *, lat, lng, radius_km=40, limit=20):
        return [
            (
                Listing(
                    source="facebook",
                    title=query,
                    price=120.0,
                    url="https://facebook/marketplace/1",
                    condition="Used",
                    image_url="https://img/1.jpg",
                ),
                {"distance_km": 12.5, "location_label": "Brooklyn, NY"},
            ),
            (
                Listing(source="facebook", title=query, price=90.0),
                {"distance_km": 3.2, "location_label": "Queens, NY"},
            ),
        ]

    monkeypatch.setattr(ApifyProvider, "search_nearby_listings", fake_search)

    resp = await client.get(
        f"/v1/cards/{_COMPOSITE}/nearby-listings"
        "?lat=40.7&lng=-73.9&radius_km=40&limit=10"
    )
    body = assert_envelope_ok(resp)
    assert body["card_id"] == _COMPOSITE
    assert body["radius_km"] == 40
    assert body["center"] == {"lat": 40.7, "lng": -73.9}
    assert len(body["listings"]) == 2
    # Closest first.
    assert body["listings"][0]["distance_km"] == 3.2
    assert body["listings"][1]["distance_km"] == 12.5
    row = body["listings"][1]
    assert row["source"] == "facebook"
    assert row["price"] == {"amount": 120.0, "currency": "USD"}
    assert row["location_label"] == "Brooklyn, NY"


@pytest.mark.asyncio
async def test_nearby_listings_empty_when_unconfigured(client, monkeypatch):
    _patch_card(monkeypatch)

    async def fake_search(self, query, *, lat, lng, radius_km=40, limit=20):
        return []

    monkeypatch.setattr(ApifyProvider, "search_nearby_listings", fake_search)

    resp = await client.get(
        f"/v1/cards/{_COMPOSITE}/nearby-listings?lat=40.7&lng=-73.9"
    )
    body = assert_envelope_ok(resp)
    assert body["listings"] == []


@pytest.mark.asyncio
async def test_nearby_listings_missing_coords_422(client):
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/nearby-listings")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_nearby_listings_invalid_card_404(client, monkeypatch):
    async def fake_search(self, query, *, lat, lng, radius_km=40, limit=20):
        return []

    monkeypatch.setattr(ApifyProvider, "search_nearby_listings", fake_search)

    resp = await client.get(
        "/v1/cards/not-a-composite/nearby-listings?lat=40.7&lng=-73.9"
    )
    assert resp.status_code == 404


def test_map_item_reads_common_keys():
    listing, geo = _map_item(
        {
            "title": "Charizard Base Set",
            "price": "$1,250.00",
            "listingUrl": "https://facebook/marketplace/abc",
            "primaryImage": "https://img/c.jpg",
            "condition": "Used - Like New",
            "city": "Austin, TX",
            "distanceKm": 8.4,
        }
    )
    assert listing.source == "facebook"
    assert listing.title == "Charizard Base Set"
    assert listing.price == 1250.0
    assert listing.url == "https://facebook/marketplace/abc"
    assert listing.image_url == "https://img/c.jpg"
    assert geo["distance_km"] == 8.4
    assert geo["location_label"] == "Austin, TX"


def test_map_item_rejects_missing_price_or_title():
    assert _map_item({"title": "No price here"}) is None
    assert _map_item({"price": 10.0}) is None
    assert _map_item("not a dict") is None
