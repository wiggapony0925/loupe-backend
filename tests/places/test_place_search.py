"""Profile location picker — the gazetteer proxy.

Location used to be a free-text box, so profiles carried "whatberel". These
cover the contract the picker relies on, with the upstream monkeypatched:
the suite must never call the live gazetteer.
"""

from __future__ import annotations

import json

import pytest

from app.services.places import place_search


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload=None, boom=False):
        self._payload = payload or {}
        self._boom = boom
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls += 1
        if self._boom:
            raise RuntimeError("gazetteer down")
        return _Resp(self._payload)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """kv_cache is shared; keep each test isolated and deterministic."""
    store: dict[str, str] = {}

    async def fake_get(key):
        return store.get(key)

    async def fake_set(key, value, ttl_seconds=0):
        store[key] = value

    monkeypatch.setattr(place_search, "kv_get", fake_get)
    monkeypatch.setattr(place_search, "kv_set", fake_set)
    return store


def _payload(*rows):
    return {"results": list(rows)}


@pytest.mark.asyncio
async def test_builds_a_city_region_country_label(monkeypatch):
    client = _Client(
        _payload(
            {
                "name": "Austin",
                "admin1": "Texas",
                "country": "United States",
                "country_code": "us",
            }
        )
    )
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    res = await place_search.search("austin")
    assert [p.label for p in res.places] == ["Austin, Texas, United States"]
    assert res.places[0].country_code == "US"


@pytest.mark.asyncio
async def test_a_city_named_after_its_region_is_not_repeated(monkeypatch):
    """ "Berlin, Berlin, Germany" reads like a bug."""
    client = _Client(
        _payload({"name": "Berlin", "admin1": "Berlin", "country": "Germany"})
    )
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    res = await place_search.search("berlin")
    assert [p.label for p in res.places] == ["Berlin, Germany"]


@pytest.mark.asyncio
async def test_near_duplicate_rows_collapse(monkeypatch):
    """The gazetteer returns the same city under several ids."""
    row = {"name": "Tokyo", "admin1": "", "country": "Japan"}
    client = _Client(_payload(row, dict(row), dict(row)))
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    res = await place_search.search("tokyo")
    assert [p.label for p in res.places] == ["Tokyo, Japan"]


@pytest.mark.asyncio
async def test_gibberish_matches_nothing(monkeypatch):
    """The whole point: a profile can no longer say "whatberel"."""
    client = _Client(_payload())
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    res = await place_search.search("whatberel")
    assert res.places == []
    assert res.degraded is False


@pytest.mark.asyncio
async def test_a_dead_gazetteer_degrades_and_never_raises(monkeypatch):
    """A picker that can't reach its source must not block a profile save."""
    monkeypatch.setattr(
        place_search.httpx, "AsyncClient", lambda **kw: _Client(boom=True)
    )

    res = await place_search.search("berlin")
    assert res.places == []
    assert res.degraded is True, "the client falls back to free text"


@pytest.mark.asyncio
async def test_repeat_queries_are_cached(monkeypatch, _no_cache):
    client = _Client(_payload({"name": "Berlin", "country": "Germany"}))
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    await place_search.search("berlin")
    await place_search.search("Berlin")  # same query, different case
    assert client.calls == 1, "a typeahead must not hammer a free upstream"


@pytest.mark.asyncio
async def test_a_one_character_query_never_reaches_upstream(monkeypatch):
    client = _Client(_payload())
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    assert (await place_search.search("b")).places == []
    assert client.calls == 0


@pytest.mark.asyncio
async def test_a_corrupt_cache_row_refetches(monkeypatch, _no_cache):
    _no_cache[place_search._cache_key("berlin")] = "{not json"
    client = _Client(_payload({"name": "Berlin", "country": "Germany"}))
    monkeypatch.setattr(place_search.httpx, "AsyncClient", lambda **kw: client)

    res = await place_search.search("berlin")
    assert [p.label for p in res.places] == ["Berlin, Germany"]
    assert json.loads(_no_cache[place_search._cache_key("berlin")])
