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
    for t in list(carousel_service._bg_tasks):
        t.cancel()
    if carousel_service._bg_tasks:
        await asyncio.gather(*list(carousel_service._bg_tasks), return_exceptions=True)
    carousel_service._bg_tasks.clear()
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()
    await close_redis()


@pytest.fixture(autouse=True)
async def _fresh_redis():
    await _reset()
    yield
    await _reset()


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
        if carousel_service._bg_tasks:
            await asyncio.gather(
                *list(carousel_service._bg_tasks), return_exceptions=True
            )


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
async def test_get_carousels_ai_path_is_non_blocking(monkeypatch) -> None:
    # The request never blocks on the model: the first call returns curated
    # instantly and generates AI in the background; a later call serves AI.
    from app.schemas.carousel import CarouselRecipe

    # A unique game so this test can't share cooldown/cache state with another.
    game = "ai-path-test"
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()
    monkeypatch.setattr(carousel_service, "configured", lambda: True)

    async def fake_gen(g: str, label: str) -> list[CarouselRecipe]:
        return [
            CarouselRecipe(id="grails", title="Grails", subtitle="s", source="value")
        ]

    monkeypatch.setattr(carousel_service, "_generate_ai", fake_gen)

    first = await carousel_service.get_carousels(game)
    assert first.source == "curated"  # instant, never blocks on the model

    # Robustly wait for the background generation to cache the AI result.
    resp = first
    for _ in range(100):
        if carousel_service._bg_tasks:
            await asyncio.gather(
                *list(carousel_service._bg_tasks), return_exceptions=True
            )
        resp = await carousel_service.get_carousels(game)
        if resp.source == "ai":
            break
        await asyncio.sleep(0.02)

    assert resp.source == "ai"
    assert [c.id for c in resp.carousels] == ["grails"]


@pytest.mark.asyncio
async def test_failing_model_backs_off(monkeypatch) -> None:
    # A failing key (e.g. 429 no-quota) is retried at most once per cooldown,
    # not on every marketplace load — so we don't spam the model.
    game = "backoff-test"  # unique game → no shared cooldown state
    carousel_service._inflight.clear()
    carousel_service._last_attempt.clear()
    monkeypatch.setattr(carousel_service, "configured", lambda: True)

    calls = {"n": 0}

    async def failing_gen(g: str, label: str):
        calls["n"] += 1
        raise RuntimeError("429 insufficient_quota")

    monkeypatch.setattr(carousel_service, "_generate_ai", failing_gen)

    await carousel_service.get_carousels(game)  # first miss → spawns (fails)
    await _drain()
    assert calls["n"] == 1  # the one attempt happened
    # Repeated loads within the cooldown must NOT spawn more model calls.
    for _ in range(3):
        resp = await carousel_service.get_carousels(game)
        await _drain()
        assert resp.source == "curated"  # always serves fast, never errors
    assert calls["n"] == 1  # backed off after the first failure
