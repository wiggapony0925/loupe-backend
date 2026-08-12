"""Storefront discovery surface: resolved carousels, rail expansion, sparklines.

These three are what a cold marketplace load actually renders. The resolved
payload is the single source of truth both clients paint (no client-side
filtering), the rail endpoint is what "view more" lands on, and sparklines are
the batched trend series behind every list row.

Every upstream is faked: the AI designer is switched off and the shelf/browse
services are monkeypatched, so nothing here reaches a network or a model.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service, carousel_service
from app.services.catalog import catalog_browse_service as browse_svc
from app.services.market import trending_service
from tests.conftest import assert_envelope_error, assert_envelope_ok


def _card(id_: str, name: str, price: float, rarity: str = "Rare") -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "rarity": rarity,
        "set_name": "Base",
        "image_url": f"https://img/{id_}.png",
        "pricing_summary": {"market": {"amount": price, "currency": "USD"}},
    }


@pytest.fixture(autouse=True)
def no_ai_designer(monkeypatch):
    """Pin serving to the curated registry.

    ``get_carousels`` spawns a real model call in the background whenever a key
    is configured — the developer's ``.env`` has one, so without this the suite
    would quietly bill an OpenAI request per test.
    """
    monkeypatch.setattr(carousel_service, "configured", lambda: False)


@pytest.fixture
def fake_shelves(monkeypatch):
    """A priced discovery pool + a catalog pool, both deterministic.

    Prices are spread across the registry's bands so several curated recipes
    (grails ≥ $250, midrange $25–150, under $25 …) actually fill.
    """
    trending = [_card(f"t{i}", f"Trend {i:02d}", 20.0 + i) for i in range(8)]
    value = [_card(f"v{i}", f"Value {i:02d}", 5.0 * (i + 1)) for i in range(80)]
    catalog = [_card(f"c{i}", f"Catalog {i:02d}", 1.0 + i) for i in range(60)]

    async def fake_shelf(
        tcg: str = "all",
        sort: str = "trending",
        max_price: float | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        cards = trending if sort == "trending" else value
        if max_price is not None:
            cards = [
                c
                for c in cards
                if c["pricing_summary"]["market"]["amount"] <= max_price
            ]
        return {"cards": cards[:limit], "source": "live"}

    async def fake_browse(
        game: str,
        page: int = 1,
        page_size: int = 24,
        *,
        sort: str = "name",
        set_id: str | None = None,
    ) -> dict[str, Any]:
        start = (page - 1) * page_size
        return {
            "cards": catalog[start : start + page_size],
            "total": len(catalog),
            "page": page,
            "source": "catalog",
        }

    monkeypatch.setattr(trending_service, "get_shelf", fake_shelf)
    monkeypatch.setattr(browse_svc, "browse_catalog", fake_browse)
    return {"trending": trending, "value": value, "catalog": catalog}


# ── GET /v1/public/carousels/resolved ─────────────────────────────────────


@pytest.mark.asyncio
async def test_resolved_carousels_arrive_as_ready_to_render_rails(client, fake_shelves):
    """The point of this endpoint: rails that already contain cards. If it
    returned recipes, each client would compile them differently and the same
    shelf would look different on web and mobile."""
    data = assert_envelope_ok(
        await client.get("/v1/public/carousels/resolved", params={"game": "pokemon"})
    )

    assert data["game"] == "pokemon"
    assert data["source"] == "curated"
    assert data["rails"], "a game with a full priced pool must resolve some rails"
    for rail in data["rails"]:
        assert rail["cards"], "an empty rail must never be served"
        assert rail["kind"] in ("cards", "catalog")


@pytest.mark.asyncio
async def test_resolved_rails_interpolate_the_game_label(client, fake_shelves):
    """Recipe copy carries a ``{label}`` placeholder. Resolving is where it gets
    filled in — a client that received the raw token would print it verbatim."""
    data = assert_envelope_ok(
        await client.get("/v1/public/carousels/resolved", params={"game": "pokemon"})
    )

    for rail in data["rails"]:
        assert "{label}" not in rail["title"]
        assert "{label}" not in rail["subtitle"]
    assert any(
        "Pokémon" in r["subtitle"] or "Pokémon" in r["title"] for r in data["rails"]
    )


@pytest.mark.asyncio
async def test_a_card_appears_on_at_most_one_rail(client, fake_shelves):
    """Themed shelves drawn from one pool overlap heavily. Without the de-dupe
    the storefront reads as the same twenty cards under six different titles."""
    data = assert_envelope_ok(
        await client.get("/v1/public/carousels/resolved", params={"game": "pokemon"})
    )

    seen: list[str] = [c["id"] for rail in data["rails"] for c in rail["cards"]]
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_thin_rails_are_dropped_rather_than_served_empty(client, monkeypatch):
    """A rail the pool can't fill is dropped server-side, so a client never has
    to decide whether to render a one-card shelf or hide it."""

    async def thin_shelf(
        tcg: str = "all",
        sort: str = "trending",
        max_price: float | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        return {"cards": [_card("only", "Lonely", 300.0)], "source": "live"}

    async def empty_browse(
        game: str,
        page: int = 1,
        page_size: int = 24,
        *,
        sort: str = "name",
        set_id: str | None = None,
    ) -> dict[str, Any]:
        return {"cards": [], "total": 0, "page": page, "source": "catalog"}

    monkeypatch.setattr(trending_service, "get_shelf", thin_shelf)
    monkeypatch.setattr(browse_svc, "browse_catalog", empty_browse)

    data = assert_envelope_ok(
        await client.get("/v1/public/carousels/resolved", params={"game": "pokemon"})
    )
    assert data["rails"] == []


@pytest.mark.asyncio
async def test_resolved_carousels_are_public(client, fake_shelves):
    resp = await client.get("/v1/public/carousels/resolved", params={"game": "pokemon"})
    assert resp.status_code == 200
    assert "Cache-Control" in resp.headers


@pytest.mark.asyncio
async def test_resolved_carousels_reject_the_mixed_pseudo_game(client, fake_shelves):
    """Carousels are per-game merchandising; ``all`` has no shelf pool, so it is
    a validation error rather than a silently empty storefront."""
    resp = await client.get("/v1/public/carousels/resolved", params={"game": "all"})
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_resolved_carousels_reject_a_game_with_no_catalog(client, fake_shelves):
    resp = await client.get("/v1/public/carousels/resolved", params={"game": "lorcana"})
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/public/carousels/rail ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_expanding_the_trending_rail_paginates_the_deep_pool(
    client, fake_shelves
):
    """ "View more" must page the FULL pool with an honest total — the teaser on
    the shelf is a slice, not the universe."""
    data = assert_envelope_ok(
        await client.get(
            "/v1/public/carousels/rail",
            params={"id": "trending", "game": "pokemon", "page": 2, "page_size": 3},
        )
    )

    assert data["id"] == "trending"
    assert data["total"] == 8
    assert data["page"] == 2
    assert [c["id"] for c in data["cards"]] == ["t3", "t4", "t5"]
    assert data["title"] == "Trending in Pokémon"


@pytest.mark.asyncio
async def test_the_trending_rail_is_the_one_rail_that_spans_every_game(
    client, fake_shelves
):
    """``game=all`` backs the mixed Search-tab rail. Only the momentum anchor
    exists at that scope, and its copy drops the game name."""
    data = assert_envelope_ok(
        await client.get(
            "/v1/public/carousels/rail", params={"id": "trending", "game": "all"}
        )
    )
    assert data["title"] == "Trending now"
    assert data["cards"]


@pytest.mark.asyncio
async def test_the_explore_rail_reports_the_real_catalog_total(client, fake_shelves):
    """Explore delegates to the browse catalog so the pager reflects the actual
    catalog size, not the handful of cards on the current page."""
    data = assert_envelope_ok(
        await client.get(
            "/v1/public/carousels/rail",
            params={"id": "explore", "game": "pokemon", "page": 1, "page_size": 5},
        )
    )

    assert data["kind"] == "catalog"
    assert data["total"] == 60
    assert len(data["cards"]) == 5


@pytest.mark.asyncio
async def test_expanding_a_recipe_rail_applies_the_same_lens_as_the_shelf(
    client, fake_shelves
):
    """The expanded view has to obey the recipe that titled it: "Grails" is a
    ≥ $250 shelf, so a $30 card must not appear behind "view more"."""
    data = assert_envelope_ok(
        await client.get(
            "/v1/public/carousels/rail",
            params={"id": "grails", "game": "pokemon", "page_size": 50},
        )
    )

    prices = [c["pricing_summary"]["market"]["amount"] for c in data["cards"]]
    assert prices, "the fake pool has cards above the grails floor"
    assert all(p >= 250 for p in prices)
    assert prices == sorted(prices, reverse=True)  # recipe sort is price_desc


@pytest.mark.asyncio
async def test_an_unknown_rail_404s_so_clients_can_degrade(client, fake_shelves):
    """AI-designed shelves expire daily; a client holding yesterday's id needs a
    404 it can fall back from, not an empty 200 that looks like a dead rail."""
    resp = await client.get(
        "/v1/public/carousels/rail",
        params={"id": "yesterdays-ai-shelf", "game": "pokemon"},
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_the_rail_endpoint_rejects_an_unsupported_game(client, fake_shelves):
    resp = await client.get(
        "/v1/public/carousels/rail", params={"id": "trending", "game": "lorcana"}
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_the_rail_endpoint_caps_page_size(client, fake_shelves):
    """An unbounded page_size turns "view more" into a full-catalog dump."""
    resp = await client.get(
        "/v1/public/carousels/rail",
        params={"id": "trending", "game": "pokemon", "page_size": 500},
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_the_rail_endpoint_requires_an_id(client, fake_shelves):
    resp = await client.get("/v1/public/carousels/rail", params={"game": "pokemon"})
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/public/sparklines ─────────────────────────────────────────────


def _history(prices: list[float | None], change_pct: float | None = 4.2) -> dict:
    return {
        "points": [
            {"date": f"2026-08-{i + 1:02d}", "price": p} for i, p in enumerate(prices)
        ],
        "summary": {"change_pct": change_pct},
    }


@pytest.fixture
def price_history(monkeypatch):
    """Record which ids the batch endpoint actually asked for."""
    asked: list[tuple[str, str]] = []
    series: dict[str, dict | None] = {}

    async def fake_history(card_id: str, range_: str = "30d") -> dict | None:
        asked.append((card_id, range_))
        return series.get(card_id)

    monkeypatch.setattr(card_search_service, "get_price_history", fake_history)
    return asked, series


@pytest.mark.asyncio
async def test_sparklines_returns_one_series_per_requested_id(client, price_history):
    """One request for a screenful of rows — the whole reason this is batched
    instead of a price-history call per visible card."""
    asked, series = price_history
    series["a"] = _history([1.0, 2.0, 3.0], change_pct=12.5)
    series["b"] = _history([9.0, 8.0], change_pct=-4.0)

    data = assert_envelope_ok(
        await client.get("/v1/public/sparklines", params={"ids": "a,b", "range": "30d"})
    )

    assert [s["card_id"] for s in data["sparklines"]] == ["a", "b"]
    assert data["sparklines"][0]["points"] == [1.0, 2.0, 3.0]
    assert data["sparklines"][0]["change_pct"] == 12.5
    assert asked == [("a", "30d"), ("b", "30d")]


@pytest.mark.asyncio
async def test_sparklines_skips_ids_with_no_history(client, price_history):
    """A row whose card has no series is simply absent — the client draws no
    sparkline rather than an empty axis, and the rest of the batch survives."""
    _asked, series = price_history
    series["known"] = _history([1.0, 2.0])

    data = assert_envelope_ok(
        await client.get("/v1/public/sparklines", params={"ids": "known,ghost"})
    )
    assert [s["card_id"] for s in data["sparklines"]] == ["known"]


@pytest.mark.asyncio
async def test_sparklines_drops_gaps_in_a_series(client, price_history):
    """A null point would land in the array as ``None`` and break the client's
    path maths — the gap is dropped instead."""
    _asked, series = price_history
    series["a"] = _history([1.0, None, 3.0])

    data = assert_envelope_ok(
        await client.get("/v1/public/sparklines", params={"ids": "a"})
    )
    assert data["sparklines"][0]["points"] == [1.0, 3.0]


@pytest.mark.asyncio
async def test_sparklines_caps_the_fan_out(client, price_history):
    """One request can otherwise fan out to as many upstream lookups as it has
    commas. The batch is capped at 24 ids regardless of what was asked for."""
    asked, series = price_history
    ids = [f"c{i}" for i in range(40)]
    for i in ids:
        series[i] = _history([1.0, 2.0])

    data = assert_envelope_ok(
        await client.get("/v1/public/sparklines", params={"ids": ",".join(ids)})
    )

    assert len(asked) == 24
    assert len(data["sparklines"]) == 24


@pytest.mark.asyncio
async def test_sparklines_ignores_blank_ids(client, price_history):
    asked, series = price_history
    series["a"] = _history([1.0])

    assert_envelope_ok(
        await client.get("/v1/public/sparklines", params={"ids": "a,, ,"})
    )
    assert [cid for cid, _ in asked] == ["a"]


@pytest.mark.asyncio
async def test_sparklines_defaults_to_the_seven_day_range(client, price_history):
    asked, series = price_history
    series["a"] = _history([1.0])

    assert_envelope_ok(await client.get("/v1/public/sparklines", params={"ids": "a"}))
    assert asked == [("a", "7d")]


@pytest.mark.asyncio
async def test_sparklines_rejects_an_unsupported_range(client, price_history):
    """Only the three cached windows are allowed — an arbitrary range would be a
    cache-busting parameter anyone could point at the upstream."""
    asked, _series = price_history
    resp = await client.get("/v1/public/sparklines", params={"ids": "a", "range": "5y"})
    assert_envelope_error(resp, expected_status=422)
    assert asked == []


@pytest.mark.asyncio
async def test_sparklines_requires_ids(client, price_history):
    resp = await client.get("/v1/public/sparklines")
    assert_envelope_error(resp, expected_status=422)
