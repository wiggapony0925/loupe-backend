"""Tests for the AI carousel service (fallback + output validation)."""

from __future__ import annotations

import asyncio

import pytest

from app.platform.redis_client import close_redis
from app.services.catalog import carousel_service
from app.services.catalog.carousel_service import _coerce


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
async def test_fallback_is_curated_empty_when_unconfigured(monkeypatch) -> None:
    # No model configured → return an empty "curated" set so the web uses its
    # own built-in strategy pool (never an error).
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    resp = await carousel_service.get_carousels("pokemon")
    assert resp.game == "pokemon"
    assert resp.source == "curated"
    assert resp.carousels == []


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch) -> None:
    monkeypatch.setattr(carousel_service, "configured", lambda: False)
    first = await carousel_service.get_carousels("magic")
    # Second call hits the cache and returns the same shape.
    second = await carousel_service.get_carousels("magic")
    assert second.source == first.source == "curated"
    assert second.game == "magic"


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
