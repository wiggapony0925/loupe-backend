"""Public storefront API (/v1/public/*) — server-side filter/sort/paginate."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service
from app.services.market import trending_service


async def _noop_cache_get(key: str) -> Any:
    """Force a cold cache so retry/call-count assertions are deterministic."""
    return None


async def _noop_cache_set(key: str, value: Any, ttl: int) -> None:
    """No-op cache write so the retry tests never persist a page."""


def _card(
    id_: str, name: str, rarity: str, set_name: str, price: float
) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "rarity": rarity,
        "set_name": set_name,
        "image_url": f"https://img/{id_}.png",
        "pricing_summary": {"market": {"amount": price, "currency": "USD"}},
    }


@pytest.mark.asyncio
async def test_public_search_filters_sorts_paginates_and_facets(client, monkeypatch):
    cards = [
        _card("a", "Bravo", "Rare", "Base", 10.0),
        _card("b", "Alpha", "Common", "Base", 2.0),
        _card("c", "Charlie", "Rare", "Jungle", 5.0),
    ]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": cards, "total": len(cards), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search",
        params={
            "q": "x",
            "rarity": "Rare",
            "sort": "price_desc",
            "page": 1,
            "page_size": 1,
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["total"] == 2  # two Rares match the filter
    assert data["page_size"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["name"] == "Bravo"  # 10.0 sorts above 5.0
    # facets come from the full pre-filter set
    assert set(data["facets"]["rarities"]) == {"Common", "Rare"}
    assert set(data["facets"]["sets"]) == {"Base", "Jungle"}


@pytest.mark.asyncio
async def test_public_search_page_two(client, monkeypatch):
    cards = [_card(str(i), f"Card {i:02d}", "Rare", "Base", float(i)) for i in range(5)]

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": cards, "total": len(cards), "source": "mixed"}

    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/public/search",
        params={"q": "x", "sort": "name", "page": 2, "page_size": 2},
    )
    data = resp.json()["data"]
    assert data["total"] == 5
    assert [c["name"] for c in data["results"]] == ["Card 02", "Card 03"]


@pytest.mark.asyncio
async def test_public_browse_pokemon_paginates(client, monkeypatch):
    from app.services.catalog import catalog_browse_service as svc

    captured: dict = {}

    async def fake_search(
        query: str, page: int = 1, page_size: int = 25, *, order_by=None, timeout_s=None
    ):
        captured["order_by"] = order_by
        return {"data": [{"id": "p1"}, {"id": "p2"}], "totalCount": 18342}

    monkeypatch.setattr(svc.pokemon_tcg, "search_cards", fake_search)
    monkeypatch.setattr(
        svc, "_from_pokemon", lambda c: {"id": c["id"], "name": c["id"]}
    )

    resp = await client.get(
        "/v1/public/browse",
        params={"game": "pokemon", "page": 3, "page_size": 24, "sort": "price_desc"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 18342  # real catalog size drives the pager
    assert data["page"] == 3
    assert [c["id"] for c in data["cards"]] == ["p1", "p2"]
    # sort maps to the upstream's native ordering
    assert captured["order_by"] == "-cardmarket.prices.averageSellPrice"


@pytest.mark.parametrize(
    ("sort", "expected_order_by"),
    [
        ("name", "name"),
        ("newest", "-set.releaseDate"),
        ("price_asc", "cardmarket.prices.averageSellPrice"),
        ("price_desc", "-cardmarket.prices.averageSellPrice"),
    ],
)
@pytest.mark.asyncio
async def test_public_browse_each_sort_returns_a_full_page(
    client, monkeypatch, sort, expected_order_by
):
    """Every BrowseSort must return rows (a full page), not just a total.

    Regression guard for the ``sort=newest`` bug where the endpoint reported
    ``total=20359`` but ``cards=[]``."""
    from app.services.catalog import catalog_browse_service as svc

    page_size = 20
    captured: dict = {}

    async def fake_search(
        query: str, page: int = 1, page_size: int = 25, *, order_by=None, timeout_s=None
    ):
        captured["order_by"] = order_by
        return {
            "data": [{"id": f"p{i}"} for i in range(page_size)],
            "totalCount": 20359,
        }

    monkeypatch.setattr(svc.pokemon_tcg, "search_cards", fake_search)
    monkeypatch.setattr(
        svc, "_from_pokemon", lambda c: {"id": c["id"], "name": c["id"]}
    )

    resp = await client.get(
        "/v1/public/browse",
        params={"game": "pokemon", "page": 1, "page_size": page_size, "sort": sort},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert data["total"] == 20359
    # The page is actually populated — length matches the page, not just total.
    assert len(data["cards"]) == page_size
    # Each sort maps to the upstream's native ordering.
    assert captured["order_by"] == expected_order_by


@pytest.mark.asyncio
async def test_public_browse_retries_empty_but_counted_page(client, monkeypatch):
    """A degraded upstream page (positive total, zero rows) is retried.

    pokemontcg.io intermittently answers heavy ``orderBy`` full-catalog queries
    with a 200 carrying a real ``totalCount`` but an empty ``data`` array. That
    is the ``total=20359, cards=[]`` "newest is broken" bug — one retry recovers
    the rows instead of surfacing an empty page."""
    from app.services.catalog import catalog_browse_service as svc

    monkeypatch.setattr(svc, "_cache_get", _noop_cache_get)
    monkeypatch.setattr(svc, "_cache_set", _noop_cache_set)
    monkeypatch.setattr(
        svc, "_from_pokemon", lambda c: {"id": c["id"], "name": c["id"]}
    )

    calls = {"n": 0}

    async def flaky_search(
        query: str, page: int = 1, page_size: int = 25, *, order_by=None, timeout_s=None
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # Degraded: count is real, but the row payload came back empty.
            return {"data": [], "totalCount": 20359}
        return {"data": [{"id": "p1"}, {"id": "p2"}], "totalCount": 20359}

    monkeypatch.setattr(svc.pokemon_tcg, "search_cards", flaky_search)

    resp = await client.get(
        "/v1/public/browse",
        params={"game": "pokemon", "page": 1, "page_size": 20, "sort": "newest"},
    )
    data = resp.json()["data"]
    assert calls["n"] == 2  # retried once
    assert data["total"] == 20359
    assert [c["id"] for c in data["cards"]] == ["p1", "p2"]  # rows recovered


@pytest.mark.asyncio
async def test_public_browse_out_of_range_page_is_not_retried(client, monkeypatch):
    """An empty page whose offset is past the end is *legit* — don't retry it."""
    from app.services.catalog import catalog_browse_service as svc

    monkeypatch.setattr(svc, "_cache_get", _noop_cache_get)
    monkeypatch.setattr(svc, "_cache_set", _noop_cache_set)

    calls = {"n": 0}

    async def empty_tail(
        query: str, page: int = 1, page_size: int = 25, *, order_by=None, timeout_s=None
    ):
        calls["n"] += 1
        # Only 30 cards total, but page 100 is requested → genuinely empty.
        return {"data": [], "totalCount": 30}

    monkeypatch.setattr(svc.pokemon_tcg, "search_cards", empty_tail)

    resp = await client.get(
        "/v1/public/browse",
        params={"game": "pokemon", "page": 100, "page_size": 20, "sort": "newest"},
    )
    data = resp.json()["data"]
    assert calls["n"] == 1  # no wasteful retry on a real end-of-catalog page
    assert data["cards"] == []


@pytest.mark.asyncio
async def test_public_browse_unsupported_game_is_empty(client):
    resp = await client.get("/v1/public/browse", params={"game": "lorcana"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["cards"] == []


@pytest.mark.asyncio
async def test_public_trending_value_sort_and_price_ceiling(client, monkeypatch):
    cards = [
        _card("a", "A", "Rare", "S", 3.0),
        _card("b", "B", "Rare", "S", 50.0),
        _card("c", "C", "Rare", "S", 1.0),
    ]

    async def fake_feed(tcg: str = "all", limit: int = 24) -> dict[str, Any]:
        return {"cards": cards, "source": "live"}

    # `sort=value` now draws from the dedicated most-valuable source; the
    # default/steals path still uses get_trending.
    monkeypatch.setattr(trending_service, "get_trending", fake_feed)
    monkeypatch.setattr(trending_service, "get_most_valuable", fake_feed)

    by_value = await client.get(
        "/v1/public/trending", params={"sort": "value", "limit": 2}
    )
    assert [c["id"] for c in by_value.json()["data"]["cards"]] == ["b", "a"]

    cheap = await client.get("/v1/public/trending", params={"max_price": 5})
    assert {c["id"] for c in cheap.json()["data"]["cards"]} == {"a", "c"}


def _valuable_cards(tcg: str, n: int = 50) -> list[dict[str, Any]]:
    """A realistic 'most valuable' pool: distinct, priced, art-bearing cards."""
    return [
        {
            **_card(
                f"{tcg}-{i}", f"{tcg.title()} {i:02d}", "Rare", "S", float(500 - i)
            ),
            "tcg": tcg,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize("tcg", ["pokemon", "magic", "yugioh"])
@pytest.mark.asyncio
async def test_public_trending_value_is_stable_across_repeats(client, monkeypatch, tcg):
    """The same (tcg, sort=value, limit) must return a consistent, non-empty
    list on every call — even when the upstream flaps.

    Regression guard for the reported bug: ``/v1/public/trending?sort=value``
    intermittently returned ``cards: []`` for a data-backed game because a
    timed-out upstream produced an empty list that, being uncached, made every
    later request re-race the same flaky provider. The fix serves the last-good
    list, so a single warm fetch keeps the rail stable.
    """
    fn = {
        "pokemon": "_valuable_pokemon",
        "magic": "_valuable_magic",
        "yugioh": "_valuable_yugioh",
    }[tcg]
    pool = _valuable_cards(tcg)

    # Cache stub that only persists the long-lived last-good *pool* keys, never
    # the per-(tcg,limit,day) envelope. That forces every repeat back through
    # the live path, so the test actually exercises the last-good fallback
    # instead of trivially serving one cached envelope.
    store: dict[str, Any] = {}

    async def cache_get(key: str) -> Any:
        return store.get(key)

    async def cache_set(key: str, value: Any, ttl: int) -> None:
        if "pool" in key:
            store[key] = value

    monkeypatch.setattr(trending_service, "_cache_get", cache_get)
    monkeypatch.setattr(trending_service, "_cache_set", cache_set)

    calls = {"n": 0}

    async def flaky(pool_size: int = 50) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return pool  # warm the last-good pool exactly once …
        raise TimeoutError  # … then every later fetch "times out"

    monkeypatch.setattr(trending_service, fn, flaky)

    lengths: list[int] = []
    for _ in range(5):
        resp = await client.get(
            "/v1/public/trending", params={"tcg": tcg, "sort": "value", "limit": 40}
        )
        assert resp.status_code == 200
        lengths.append(len(resp.json()["data"]["cards"]))

    assert calls["n"] >= 2  # repeats really hit the (now failing) live path
    assert lengths == [40] * 5  # consistent, full, and never an empty blip


@pytest.mark.asyncio
async def test_most_valuable_never_caches_empty_and_recovers(monkeypatch):
    """An empty/error result is never cached, and a later success heals the rail.

    Directly exercises the service: a failed upstream must not write the daily
    envelope (otherwise the empty list would be served for the whole TTL), and
    once a fetch succeeds the last-good pool keeps subsequent failures non-empty.
    """
    store: dict[str, Any] = {}

    async def cache_get(key: str) -> Any:
        return store.get(key)

    async def cache_set(key: str, value: Any, ttl: int) -> None:
        store[key] = value

    monkeypatch.setattr(trending_service, "_cache_get", cache_get)
    monkeypatch.setattr(trending_service, "_cache_set", cache_set)

    pool = _valuable_cards("pokemon")
    seq = {"n": 0}

    async def flaky(pool_size: int = 50) -> list[dict[str, Any]]:
        seq["n"] += 1
        if seq["n"] == 2:  # only the 2nd attempt succeeds
            return pool
        raise TimeoutError

    monkeypatch.setattr(trending_service, "_valuable_pokemon", flaky)

    # 1) Cold + failing upstream → empty, and nothing is cached.
    first = await trending_service.get_most_valuable(tcg="pokemon", limit=40)
    assert first["cards"] == []
    assert first["source"] == "empty"
    assert store == {}  # neither the envelope nor a pool was written

    # 2) Upstream recovers → real list, last-good pool now warm.
    second = await trending_service.get_most_valuable(tcg="pokemon", limit=39)
    assert len(second["cards"]) == 39
    assert "loupe:cards:valuablepool:pokemon" in store

    # 3) Upstream fails again → served from last-good, still non-empty.
    third = await trending_service.get_most_valuable(tcg="pokemon", limit=38)
    assert len(third["cards"]) == 38


@pytest.mark.asyncio
async def test_public_trending_drops_priceless_cards(client, monkeypatch):
    # A card with no pricing_summary must never reach a shopping rail.
    priced = _card("p", "Priced", "Rare", "S", 4.0)
    priceless = {
        "id": "x",
        "name": "Unpriced",
        "rarity": "Rare",
        "set_name": "S",
        "image_url": "https://img/x.png",
        "pricing_summary": None,
    }

    async def fake_feed(tcg: str = "all", limit: int = 24) -> dict[str, Any]:
        return {"cards": [priced, priceless], "source": "live"}

    monkeypatch.setattr(trending_service, "get_trending", fake_feed)
    monkeypatch.setattr(trending_service, "get_most_valuable", fake_feed)

    resp = await client.get("/v1/public/trending")
    ids = [c["id"] for c in resp.json()["data"]["cards"]]
    assert ids == ["p"]  # the priceless card is filtered out


@pytest.mark.asyncio
async def test_public_games_tags(client):
    """One backend-driven list of category tags for both clients — availability
    derived from real catalog capability (not per-client hardcoding)."""
    resp = await client.get("/v1/public/games")
    assert resp.status_code == 200
    tags = {t["key"]: t for t in resp.json()["data"]["tags"]}

    # Data-backed games are live; provider-less games are "soon".
    assert tags["all"]["status"] == "live"
    assert tags["pokemon"]["status"] == "live"
    assert tags["onepiece"]["status"] == "live"  # cached catalog → not stale "Soon"
    assert tags["lorcana"]["status"] == "soon"
    assert tags["sports"]["status"] == "soon"

    # Games vs content sections are distinguished so the client can badge them.
    assert tags["pokemon"]["kind"] == "game"
    assert tags["sealed"]["kind"] == "section"
    # Canonical labels — the client renders them verbatim (no per-client drift).
    assert tags["yugioh"]["label"] == "Yu-Gi-Oh!"
