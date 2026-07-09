"""Tests for the AI carousel service (fallback + output validation)."""

from __future__ import annotations

import asyncio

import pytest

from app.platform.redis_client import close_redis
from app.schemas.carousel import CarouselRecipe, CarouselResponse
from app.services.catalog import carousel_service
from app.services.catalog.carousel_service import _apply_lens, _coerce


def _card(name: str, price: float | None, rarity: str | None = None) -> dict:
    ps = None if price is None else {"market": {"amount": price, "currency": "USD"}}
    return {"id": name, "name": name, "rarity": rarity, "pricing_summary": ps}


async def _reset() -> None:
    """Drain any leaked background generation tasks + clear module state so one
    test never pollutes the next (in-flight/cooldown guards, cache)."""
    real = [t for t in carousel_service._bg_tasks if isinstance(t, asyncio.Task)]
    for t in real:
        t.cancel()
    if real:
        await asyncio.gather(*real, return_exceptions=True)
    carousel_service._bg_tasks.clear()
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()
    await close_redis()


@pytest.fixture(autouse=True)
async def _fresh_redis():
    await _reset()
    yield
    await _reset()


@pytest.mark.asyncio
async def test_priced_game_serves_curated_pool_when_unconfigured(monkeypatch) -> None:
    # No model configured → serve the canonical curated pool (the single source
    # of truth both clients render), NOT an empty set. This is what makes the
    # endpoint work without any AI key configured.
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    resp = await carousel_service.get_carousels("pokemon")
    assert resp.game == "pokemon"
    assert resp.source == "curated"
    assert resp.carousels, "priced game must have a non-empty curated pool"
    ids = {c.id for c in resp.carousels}
    # A representative spread of the ported web shelves must be present.
    assert {"grails", "steals5", "rainbow", "blue-chips"} <= ids


@pytest.mark.asyncio
async def test_catalog_only_game_has_empty_curated_pool(monkeypatch) -> None:
    # Catalog-only games (no price feed; the catalog rail can't filter by
    # rarity) get NO curated shelves — they'd just reorder the same cards. They
    # lean on the clients' structural anchors instead.
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    for game in ("digimon", "onepiece", "lorcana"):
        resp = await carousel_service.get_carousels(game)
        assert resp.source == "curated"
        assert resp.carousels == [], f"{game} should have no priced shelves"


def test_curated_for_pool_shape() -> None:
    # Priced games share one pool; catalog-only / unknown games get nothing.
    pool = carousel_service._curated_for("pokemon")
    assert carousel_service._curated_for("magic") == pool
    assert carousel_service._curated_for("yugioh") == pool
    assert carousel_service._curated_for("digimon") == []
    assert carousel_service._curated_for("onepiece") == []
    assert carousel_service._curated_for("sports") == []
    # Every recipe validates and keeps the {label} placeholder convention so the
    # clients interpolate their own game label.
    assert all(r.source in {"value", "trending", "catalog"} for r in pool)
    assert any("{label}" in r.subtitle for r in pool)
    # Ids are unique (they key the client rails).
    assert len({r.id for r in pool}) == len(pool)


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch) -> None:
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    first = await carousel_service.get_carousels("magic")
    # Second call hits the cache and returns the same shape.
    second = await carousel_service.get_carousels("magic")
    assert second.source == first.source == "curated"
    assert second.game == "magic"


def test_apply_lens_filters_and_sorts() -> None:
    cards = [_card("a", 300), _card("b", 100), _card("c", None), _card("d", 5)]
    # price band ≥ 250, drops priceless + cheap
    out = _apply_lens(
        cards, CarouselRecipe(id="g", title="G", subtitle="s", priceMin=250)
    )
    assert [c["id"] for c in out] == ["a"]
    # cheapest-first with a cap
    out = _apply_lens(
        cards,
        CarouselRecipe(id="s", title="S", subtitle="s", priceMax=150, sort="price_asc"),
    )
    assert [c["id"] for c in out] == ["d", "b"]


def test_apply_lens_rarity_pattern() -> None:
    cards = [_card("a", 10, "Secret Rare"), _card("b", 10, "Common"), _card("c", 10)]
    out = _apply_lens(
        cards,
        CarouselRecipe(id="r", title="R", subtitle="s", rarityPattern="secret|rainbow"),
    )
    assert [c["id"] for c in out] == ["a"]


@pytest.mark.asyncio
async def test_resolve_carousels_builds_and_drops_thin_rails(monkeypatch) -> None:
    # The resolver runs recipes against the shelf/catalog server-side, keeps
    # rails with ≥4 cards, drops thin ones, interpolates {label}, and appends an
    # explore rail. Mock the data sources so it's deterministic + offline.
    async def fake_get_carousels(game: str) -> CarouselResponse:
        return CarouselResponse(
            game=game,
            source="curated",
            carousels=[
                # populated: 5 cards ≥ $150
                CarouselRecipe(
                    id="premium", title="Premium {label}", subtitle="s", priceMin=150
                ),
                # thin: nothing ≤ $5 → dropped
                CarouselRecipe(id="steals5", title="Steals", subtitle="s", priceMax=5),
            ],
        )

    async def fake_shelf(game, sort, max_price=None, limit=48):
        if sort == "trending":
            return [_card(f"t{i}", 500) for i in range(6)]  # anchor shows
        return [_card(f"v{i}", 200) for i in range(5)]  # all $200

    async def fake_catalog(game, limit=24):
        return [_card(f"cat{i}", None) for i in range(limit)]

    monkeypatch.setattr(carousel_service, "get_carousels", fake_get_carousels)
    monkeypatch.setattr(carousel_service, "_shelf_cards", fake_shelf)
    monkeypatch.setattr(carousel_service, "_catalog_cards", fake_catalog)
    monkeypatch.setattr(carousel_service, "_resolved_cache_get", lambda g: _none())
    monkeypatch.setattr(carousel_service, "_resolved_cache_set", lambda r: _none())

    resolved = await carousel_service.resolve_carousels("pokemon")
    ids = [r.id for r in resolved.rails]
    assert ids == ["trending", "premium", "explore"]  # steals5 dropped (thin)
    premium = next(r for r in resolved.rails if r.id == "premium")
    assert premium.title == "Premium Pokémon"  # {label} interpolated
    assert len(premium.cards) == 5


async def _none() -> None:
    return None


def test_coerce_drops_invalid_and_dedupes() -> None:
    out = _coerce(
        [
            {"id": "grails", "title": "Grails", "subtitle": "s", "source": "value"},
            {"id": "grails", "title": "dup", "subtitle": "s"},  # duplicate id
            {"id": "missing-copy"},  # missing required title/subtitle
            {"id": "bad-src", "title": "X", "subtitle": "s", "source": "nope"},
            {
                "id": "rares",
                "title": "Rainbow rares",
                "subtitle": "s",
                "source": "value",
                "rarityPattern": "rainbow|secret",
                "sort": "price_desc",
            },
        ]
    )
    ids = [r.id for r in out]
    assert ids == ["grails", "rares"]  # dup, missing, and bad-source dropped
    assert out[1].rarityPattern == "rainbow|secret"


def test_parse_recipes_handles_openai_object_wrapper() -> None:
    # OpenAI's JSON mode returns an object; we ask for {"shelves":[...]}.
    text = (
        '{"shelves":[{"id":"grails","title":"Grails","subtitle":"s",'
        '"source":"value","priceMin":250,"sort":"price_desc"}]}'
    )
    out = carousel_service._parse_recipes(text)
    assert [r.id for r in out] == ["grails"]
    assert out[0].priceMin == 250


def test_parse_recipes_tolerates_fenced_bare_array() -> None:
    text = '```json\n[{"id":"a","title":"A","subtitle":"s","source":"catalog"}]\n```'
    out = carousel_service._parse_recipes(text)
    assert [r.id for r in out] == ["a"]


@pytest.mark.asyncio
async def test_get_carousels_miss_is_curated_and_kicks_generation(monkeypatch) -> None:
    # On a cache miss the request returns curated INSTANTLY (never blocks on the
    # model) and kicks generation in the background. Deterministic: we observe
    # that _spawn_generation is invoked, not that the fire-and-forget task runs.
    game = "ai-path-test"
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()
    monkeypatch.setattr(carousel_service, "configured", lambda: True)

    spawned: list[str] = []
    monkeypatch.setattr(
        carousel_service, "_spawn_generation", lambda g, label: spawned.append(g)
    )

    resp = await carousel_service.get_carousels(game)
    assert resp.source == "curated"  # instant, never blocks on the model
    assert spawned == [game]  # generation kicked off in the background


@pytest.mark.asyncio
async def test_get_carousels_serves_cached_ai() -> None:
    # The read path: once an AI result is cached, get_carousels serves it.
    from app.schemas.carousel import CarouselRecipe, CarouselResponse

    game = "cached-ai-test"
    cached = CarouselResponse(
        game=game,
        source="ai",
        carousels=[
            CarouselRecipe(id="grails", title="Grails", subtitle="s", source="value")
        ],
    )
    await carousel_service._cache_set(cached, 3600)

    out = await carousel_service.get_carousels(game)
    assert out.source == "ai"
    assert [c.id for c in out.carousels] == ["grails"]


def test_spawn_generation_respects_cooldown(monkeypatch) -> None:
    # The cooldown guard is a synchronous check inside _spawn_generation: a
    # second call within the cooldown must NOT create another task (so a failing
    # / no-quota key isn't retried on every load). Tested without running the
    # fire-and-forget task, which is inherently timing-dependent.
    game = "backoff-test"
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()

    # Drive the monotonic clock ourselves, pinned BELOW the cooldown window,
    # to reproduce a freshly booted host (monotonic counts from boot). The old
    # `_last_attempt.get(game, 0.0)` default made `monotonic() - 0.0 < cooldown`
    # true here and wrongly suppressed the FIRST attempt (the CI flake); the
    # None-sentinel guard fixes it.
    now = {"t": 120.0}  # 120s uptime, well below the 600s cooldown window
    monkeypatch.setattr(carousel_service.time, "monotonic", lambda: now["t"])

    created = {"n": 0}

    class _DummyTask:
        def add_done_callback(self, _cb: object) -> None:
            pass

        def cancel(self) -> None:  # so fixture teardown can cancel it
            pass

    def fake_create_task(coro: object) -> _DummyTask:
        created["n"] += 1
        coro.close()  # type: ignore[attr-defined]  # avoid "never awaited" warning
        return _DummyTask()

    monkeypatch.setattr(carousel_service.asyncio, "create_task", fake_create_task)

    carousel_service._spawn_generation(game, "Label")  # cold → spawns
    assert created["n"] == 1
    # Simulate the first attempt having finished, so only the COOLDOWN (not the
    # in-flight guard) can block the retry.
    carousel_service._inflight.clear()
    carousel_service._spawn_generation(game, "Label")  # within cooldown → no-op
    assert created["n"] == 1
