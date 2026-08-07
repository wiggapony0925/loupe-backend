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


# ── Community reviews (Resy-for-card-shops) ──


@pytest.mark.asyncio
async def test_store_detail_and_reviews_roundtrip(
    client, db_session, created_user, auth_headers, monkeypatch
):
    """Detail 404s for an unseen store; after a search caches it, a
    collector with a handle can review it, the aggregate lands on both the
    detail AND the nearby list, and re-posting edits instead of stacking."""
    from app.services.stores import store_photos

    async def fake_overpass(lat, lng, radius_m):
        return [_element(id=77, name="Review Cards", shop="games", lat=lat, lon=lng)]

    async def no_photo(store_id, *, osm_image, website):
        return None

    monkeypatch.setattr(store_locator, "_fetch_overpass", fake_overpass)
    monkeypatch.setattr(store_photos, "photo_for", no_photo)

    missing = await client.get("/v1/public/stores/osm:node:999999")
    assert missing.status_code == 404

    listed = assert_envelope_ok(
        await client.get("/v1/public/stores/nearby", params={"lat": 40.0, "lng": -73.0})
    )
    store_id = listed["stores"][0]["id"]
    assert listed["stores"][0]["rating"] is None
    assert listed["stores"][0]["review_count"] == 0

    # Reviewing requires a claimed handle (same gate as following).
    ungated = await client.put(
        f"/v1/public/stores/{store_id}/review",
        json={"rating": 5, "body": "Great singles selection"},
        headers=auth_headers,
    )
    assert ungated.status_code == 409

    await client.put(
        "/v1/social/me", json={"username": "shopreviewer"}, headers=auth_headers
    )
    written = assert_envelope_ok(
        await client.put(
            f"/v1/public/stores/{store_id}/review",
            json={"rating": 5, "body": "Great singles selection"},
            headers=auth_headers,
        )
    )
    assert written["rating"] == 5
    assert written["username"] == "shopreviewer"
    assert written["is_mine"] is True

    detail = assert_envelope_ok(await client.get(f"/v1/public/stores/{store_id}"))
    assert detail["store"]["rating"] == 5.0
    assert detail["store"]["review_count"] == 1
    assert detail["reviews"][0]["body"] == "Great singles selection"
    # Anonymous readers see the review but never own it.
    assert detail["reviews"][0]["is_mine"] is False

    # Re-posting EDITS (one review per collector per store).
    edited = assert_envelope_ok(
        await client.put(
            f"/v1/public/stores/{store_id}/review",
            json={"rating": 3, "body": "Prices went up"},
            headers=auth_headers,
        )
    )
    assert edited["rating"] == 3
    again = assert_envelope_ok(await client.get(f"/v1/public/stores/{store_id}"))
    assert again["store"]["review_count"] == 1
    assert again["store"]["rating"] == 3.0

    # Ratings ride the nearby list too.
    relisted = assert_envelope_ok(
        await client.get("/v1/public/stores/nearby", params={"lat": 40.0, "lng": -73.0})
    )
    assert relisted["stores"][0]["review_count"] == 1

    # Out-of-range ratings are rejected; deletion is idempotent.
    bad = await client.put(
        f"/v1/public/stores/{store_id}/review",
        json={"rating": 9},
        headers=auth_headers,
    )
    assert bad.status_code == 422
    assert (
        await client.delete(
            f"/v1/public/stores/{store_id}/review", headers=auth_headers
        )
    ).status_code == 204
    assert (
        await client.delete(
            f"/v1/public/stores/{store_id}/review", headers=auth_headers
        )
    ).status_code == 204
    cleared = assert_envelope_ok(await client.get(f"/v1/public/stores/{store_id}"))
    assert cleared["store"]["review_count"] == 0


def test_og_image_parsing_and_absolutize():
    """og:image extraction handles both attribute orders + relative URLs."""
    from app.services.stores.store_photos import _OG_RE, _OG_RE_REVERSED, _absolutize

    html = '<meta property="og:image" content="/img/shop.jpg">'
    assert _OG_RE.search(html).group(1) == "/img/shop.jpg"
    reversed_html = '<meta content="https://x.example/a.png" name="og:image">'
    assert _OG_RE_REVERSED.search(reversed_html).group(1) == "https://x.example/a.png"
    assert (
        _absolutize("/img/shop.jpg", "https://shop.example/about")
        == "https://shop.example/img/shop.jpg"
    )
    assert (
        _absolutize("//cdn.example/a.png", "https://s.example")
        == "https://cdn.example/a.png"
    )
    assert _absolutize("data:image/png;base64,AAA", "https://s.example") is None


@pytest.mark.asyncio
async def test_cached_search_still_indexes_stores_for_detail(monkeypatch):
    """A store found via the CACHED grid row must still be resolvable by id —
    otherwise any area searched before per-store indexing existed 404s on
    detail until its 24 h grid row expires."""
    kv: dict[str, str] = {}

    async def fake_kv_get(key):
        return kv.get(key)

    async def fake_kv_set(key, value, ttl_seconds):
        kv[key] = value

    async def fake_overpass(lat, lng, radius_m):
        return [_element(id=4242, name="Cached Cards", shop="games", lat=lat, lon=lng)]

    monkeypatch.setattr(store_locator, "kv_get", fake_kv_get)
    monkeypatch.setattr(store_locator, "kv_set", fake_kv_set)
    monkeypatch.setattr(store_locator, "_fetch_overpass", fake_overpass)

    first = await store_locator.nearby_stores(10.0, 10.0, 15)
    assert first.source == "live"
    store_id = first.stores[0].id

    # Simulate a grid row cached BEFORE per-store indexing shipped.
    for key in [k for k in kv if k.startswith("stores:one:")]:
        del kv[key]

    second = await store_locator.nearby_stores(10.0, 10.0, 15)
    assert second.source == "cached"
    assert await store_locator.store_by_id(store_id) is not None
