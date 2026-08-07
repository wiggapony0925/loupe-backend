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

    async def no_photo(store_id, *, osm_image, website, wikidata=None):
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


@pytest.mark.asyncio
async def test_saved_places_roundtrip(
    client, db_session, created_user, auth_headers, monkeypatch
):
    """Heart a shop → it lands in saved places, the flag rides the nearby
    list and the detail payload, saving twice is idempotent, and the route
    'saved' is never mistaken for a store id."""
    from app.services.stores import store_photos

    async def fake_overpass(lat, lng, radius_m):
        return [_element(id=555, name="Saved Cards", shop="games", lat=lat, lon=lng)]

    async def no_photo(store_id, *, osm_image, website, wikidata=None):
        return None

    monkeypatch.setattr(store_locator, "_fetch_overpass", fake_overpass)
    monkeypatch.setattr(store_photos, "photo_for", no_photo)

    listed = assert_envelope_ok(
        await client.get(
            "/v1/public/stores/nearby",
            params={"lat": 41.0, "lng": -72.0},
            headers=auth_headers,
        )
    )
    store_id = listed["stores"][0]["id"]
    assert listed["stores"][0]["is_saved"] is False

    # "saved" must resolve to the LIST route, not be read as a store id.
    empty = assert_envelope_ok(
        await client.get("/v1/public/stores/saved", headers=auth_headers)
    )
    assert empty["stores"] == []

    saved = assert_envelope_ok(
        await client.put(f"/v1/public/stores/{store_id}/save", headers=auth_headers)
    )
    assert saved["is_saved"] is True
    # Idempotent.
    again = assert_envelope_ok(
        await client.put(f"/v1/public/stores/{store_id}/save", headers=auth_headers)
    )
    assert again["is_saved"] is True

    mine = assert_envelope_ok(
        await client.get("/v1/public/stores/saved", headers=auth_headers)
    )
    assert [s["id"] for s in mine["stores"]] == [store_id]
    assert mine["stores"][0]["is_saved"] is True

    # The flag rides both the detail payload and the nearby list.
    detail = assert_envelope_ok(
        await client.get(f"/v1/public/stores/{store_id}", headers=auth_headers)
    )
    assert detail["store"]["is_saved"] is True
    relisted = assert_envelope_ok(
        await client.get(
            "/v1/public/stores/nearby",
            params={"lat": 41.0, "lng": -72.0},
            headers=auth_headers,
        )
    )
    assert relisted["stores"][0]["is_saved"] is True

    # Signed-out callers never see someone else's saves.
    anon = assert_envelope_ok(
        await client.get("/v1/public/stores/nearby", params={"lat": 41.0, "lng": -72.0})
    )
    assert anon["stores"][0]["is_saved"] is False
    assert (await client.get("/v1/public/stores/saved")).status_code == 401

    removed = assert_envelope_ok(
        await client.delete(f"/v1/public/stores/{store_id}/save", headers=auth_headers)
    )
    assert removed["is_saved"] is False
    cleared = assert_envelope_ok(
        await client.get("/v1/public/stores/saved", headers=auth_headers)
    )
    assert cleared["stores"] == []


@pytest.mark.asyncio
async def test_empty_results_are_never_cached(monkeypatch):
    """Overpass answers 200 with an empty list when its own timeout trips.
    Caching that locks a real neighbourhood to 'no shops' for 24 h — which
    is exactly what happened to a live area."""
    kv: dict[str, str] = {}
    calls = {"n": 0}

    async def fake_kv_get(key):
        return kv.get(key)

    async def fake_kv_set(key, value, ttl_seconds):
        kv[key] = value

    async def empty_then_full(lat, lng, radius_m):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # the slow-Overpass case
        return [_element(id=9, name="Late Cards", shop="games", lat=lat, lon=lng)]

    monkeypatch.setattr(store_locator, "kv_get", fake_kv_get)
    monkeypatch.setattr(store_locator, "kv_set", fake_kv_set)
    monkeypatch.setattr(store_locator, "_fetch_overpass", empty_then_full)

    first = await store_locator.nearby_stores(5.0, 5.0, 15)
    assert first.stores == []
    # Nothing about that emptiness may be persisted.
    assert not [k for k in kv if k.startswith("stores:nearby:")]

    second = await store_locator.nearby_stores(5.0, 5.0, 15)
    assert [s.name for s in second.stores] == ["Late Cards"]


@pytest.mark.asyncio
async def test_name_net_failure_does_not_lose_tag_results(monkeypatch):
    """The name query is the expensive half; in a dense city it times out.
    That must cost only its own extra matches, never the whole search —
    which is what blanked a live area."""
    calls = {"n": 0}

    async def flaky(query: str):
        calls["n"] += 1
        if '"name"~' in query:
            raise TimeoutError("overpass 504")  # the expensive net
        return [_element(id=1, name="Tag Cards", shop="games", lat=1.0, lon=1.0)]

    monkeypatch.setattr(store_locator, "_run_query", flaky)
    els = await store_locator._fetch_overpass(1.0, 1.0, 12000)
    assert [e["tags"]["name"] for e in els] == ["Tag Cards"]
    assert calls["n"] == 2  # both were attempted


@pytest.mark.asyncio
async def test_all_queries_failing_still_raises(monkeypatch):
    """If nothing comes back at all the caller must see the failure, so the
    area reports 'unavailable' rather than being cached as empty."""

    async def dead(query: str):
        raise TimeoutError("overpass down")

    monkeypatch.setattr(store_locator, "_run_query", dead)
    with pytest.raises(TimeoutError):
        await store_locator._fetch_overpass(1.0, 1.0, 12000)


# ── Upstream resilience ───────────────────────────────────────────────────
# These lock in the fixes for "it takes forever to find stores": mirrors are
# hedged rather than tried one-after-another, and a mirror serving a REGIONAL
# extract must never win the race with a fast empty answer.


@pytest.mark.asyncio
async def test_hedges_onto_next_mirror_when_the_first_stalls(monkeypatch):
    """A stalled mirror costs the hedge delay, not its full timeout."""
    import asyncio

    monkeypatch.setattr(store_locator, "HEDGE_DELAY_S", 0.05)
    monkeypatch.setattr(store_locator, "_preferred_url", None)

    async def fake_post(client, url, query):
        if "openstreetmap.fr" in url:
            await asyncio.sleep(30)  # the sick mirror
        return [_element(name="Hedged Hobby")]

    monkeypatch.setattr(store_locator, "_post_query", fake_post)
    elements = await asyncio.wait_for(store_locator._run_query("q"), timeout=5)
    assert elements[0]["tags"]["name"] == "Hedged Hobby"


@pytest.mark.asyncio
async def test_empty_mirror_never_beats_a_real_answer(monkeypatch):
    """overpass.osm.ch answers US queries with a fast, confident, WRONG 0.

    It returned HTTP 200 + zero elements in under a second, so a plain
    first-success race preferred it and the map came back empty.
    """
    import asyncio

    monkeypatch.setattr(store_locator, "HEDGE_DELAY_S", 0.05)
    monkeypatch.setattr(store_locator, "_preferred_url", None)

    async def fake_post(client, url, query):
        if "openstreetmap.fr" in url:
            await asyncio.sleep(0.3)  # correct but slower
            return [_element(name="Real Card Shop")]
        return []  # regional extract: instant and empty

    monkeypatch.setattr(store_locator, "_post_query", fake_post)
    elements = await store_locator._run_query("q")
    assert [e["tags"]["name"] for e in elements] == ["Real Card Shop"]


@pytest.mark.asyncio
async def test_all_empty_is_still_a_valid_empty_answer(monkeypatch):
    """When every mirror agrees there's nothing, that's an answer, not an error."""

    async def fake_post(client, url, query):
        return []

    monkeypatch.setattr(store_locator, "_post_query", fake_post)
    monkeypatch.setattr(store_locator, "HEDGE_DELAY_S", 0.01)
    assert await store_locator._run_query("q") == []


@pytest.mark.asyncio
async def test_slow_name_net_does_not_hold_up_the_tag_results(monkeypatch):
    """The name net is a bonus; it must never gate first paint."""
    import asyncio

    monkeypatch.setattr(store_locator, "NAME_GRACE_S", 0.05)

    async def fake_run(query):
        if '"name"~' in query:
            await asyncio.sleep(30)
        return [_element(name="Tag Net Shop")]

    monkeypatch.setattr(store_locator, "_run_query", fake_run)
    elements = await asyncio.wait_for(
        store_locator._fetch_overpass(40.7, -74.0, 15000), timeout=5
    )
    assert [e["tags"]["name"] for e in elements] == ["Tag Net Shop"]


def test_name_query_has_no_craft_selector():
    """The craft selector took this query from 4s to a 40s+ timeout."""
    assert '"craft"' not in store_locator._name_query(40.7, -74.0, 15000)


def test_laundromats_are_not_card_shops():
    """ "Showcase Card-op Laundr-o-mat" matched \\bcard\\b and shipped as a store."""
    assert store_locator._category_for("laundry", "Card-op Laundr-o-mat") is None
    assert store_locator._category_for("gift", "Hallmark Greeting Cards") is None
    # A real card shop, and a core-tagged shop, both survive the filter.
    assert store_locator._category_for("", "Card Connection") == "Card & game store"


@pytest.mark.asyncio
async def test_chain_branches_share_one_photo_lookup(monkeypatch):
    """Seven GameStops share a brand entity but have distinct store URLs —
    keying the dedupe on both fields fetched the same entity seven times."""
    from app.services.stores import store_photos

    calls: list[str] = []

    async def fake_wikimedia(qid: str):
        calls.append(qid)
        return f"https://commons.example/{qid}.jpg"

    monkeypatch.setattr(store_photos, "_wikimedia_image", fake_wikimedia)
    monkeypatch.setattr(store_photos, "kv_get", lambda *a, **k: _none())
    monkeypatch.setattr(store_photos, "kv_set", lambda *a, **k: _none())

    class Store:
        def __init__(self, i):
            self.id = f"node:{i}"
            self.photo_url = None
            self.website = f"https://gamestop.com/store/{i}"  # distinct per branch
            self.wikidata_id = "Q202210"  # same brand

    stores = [Store(i) for i in range(7)]
    await store_photos.photos_for_many(stores, deadline_s=5)

    assert calls == ["Q202210"], "chain must resolve exactly once"
    assert all(s.photo_url == "https://commons.example/Q202210.jpg" for s in stores)


async def _none():
    return None
