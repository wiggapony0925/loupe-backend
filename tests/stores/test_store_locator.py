"""Card-shop locator: parsing/ranking offline, caching, route contract.

Overpass is always monkeypatched — the suite must never hit the live API.
"""

from __future__ import annotations

import pytest

from app.services.stores import store_locator
from tests.conftest import assert_envelope_ok


def _element(
    *,
    id: int = 1,
    typ: str = "node",
    name: str = "Card Castle",
    shop: str = "games",
    lat: float = 40.71,
    lon: float = -74.0,
    tags: dict | None = None,
) -> dict:
    base = {
        "type": typ,
        "id": id,
        "lat": lat,
        "lon": lon,
        "tags": {"shop": shop, "name": name, **(tags or {})},
    }
    if typ != "node":
        base.pop("lat"), base.pop("lon")
        base["center"] = {"lat": lat, "lon": lon}
    return base


@pytest.mark.asyncio
async def test_nearby_parses_ranks_and_caches(monkeypatch):
    calls = {"n": 0}

    async def fake_overpass(lat, lng, radius_m):
        calls["n"] += 1
        return [
            # A toy store CLOSER than the card store — ranking must still
            # put the dedicated card store first.
            _element(id=1, name="Toys R Fun", shop="toys", lat=40.7105, lon=-74.0),
            _element(id=2, name="Card Castle", shop="games", lat=40.72, lon=-74.0),
            # Same shop mapped as node + way → dedupe keeps one.
            _element(
                id=3, typ="way", name="Card Castle", shop="games", lat=40.72, lon=-74.0
            ),
            # A supermarket named nothing card-like → dropped.
            _element(id=4, name="MegaMart", shop="supermarket"),
            # Nameless → dropped.
            {
                "type": "node",
                "id": 5,
                "lat": 40.71,
                "lon": -74.0,
                "tags": {"shop": "games"},
            },
            # Any-shop-tag but card name → kept as "May carry cards"?
            _element(
                id=6,
                name="Pokemon Corner",
                shop="gift",
                tags={
                    "website": "https://pokecorner.example",
                    "addr:street": "Main St",
                    "addr:housenumber": "5",
                    "addr:city": "NYC",
                },
            ),
        ]

    monkeypatch.setattr(store_locator, "_fetch_overpass", fake_overpass)
    kv: dict[str, str] = {}

    async def fake_kv_get(key):
        return kv.get(key)

    async def fake_kv_set(key, value, ttl_seconds):
        kv[key] = value

    monkeypatch.setattr(store_locator, "kv_get", fake_kv_get)
    monkeypatch.setattr(store_locator, "kv_set", fake_kv_set)

    result = await store_locator.nearby_stores(40.71, -74.0, 10)
    assert result.source == "live"
    names = [s.name for s in result.stores]
    assert names[0] == "Card Castle"  # dedicated store outranks nearer toy shop
    assert names.count("Card Castle") == 1  # node+way deduped
    assert "MegaMart" not in names
    poke = next(s for s in result.stores if s.name == "Pokemon Corner")
    # A gift shop with a Pokémon name is a LIKELY carrier, not a certainty.
    assert poke.category == "May carry cards"
    assert poke.address == "5 Main St, NYC"
    assert poke.website == "https://pokecorner.example"

    # Second call inside the same grid cell must come from the cache.
    second = await store_locator.nearby_stores(40.7101, -74.0004, 10)
    assert second.source == "cached"
    assert [s.name for s in second.stores] == names
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_nearby_survives_overpass_outage(monkeypatch):
    async def down(lat, lng, radius_m):
        raise RuntimeError("overpass 504")

    monkeypatch.setattr(store_locator, "_fetch_overpass", down)
    result = await store_locator.nearby_stores(1.0, 1.0, 5)
    assert result.source == "unavailable"
    assert result.stores == []


@pytest.mark.asyncio
async def test_public_route_no_auth(client, monkeypatch):
    async def fake_overpass(lat, lng, radius_m):
        return [_element(name="Route Cards", shop="games", lat=lat + 0.001, lon=lng)]

    monkeypatch.setattr(store_locator, "_fetch_overpass", fake_overpass)
    resp = await client.get(
        "/v1/public/stores/nearby", params={"lat": 34.05, "lng": -118.24}
    )
    data = assert_envelope_ok(resp)
    assert data["stores"][0]["name"] == "Route Cards"
    assert data["stores"][0]["category"] == "Card & game store"
    assert data["stores"][0]["distance_km"] < 1

    # Bounds are validated — a latitude off the globe is a 422.
    bad = await client.get("/v1/public/stores/nearby", params={"lat": 999, "lng": 0})
    assert bad.status_code == 422
