"""Tests for the stale-while-revalidate / single-flight cache."""

from __future__ import annotations

import asyncio

import pytest

from app.platform import cache_swr
from app.platform.cache_swr import swr_get_or_refresh
from app.platform.redis_client import close_redis


async def _reset() -> None:
    for t in list(cache_swr._bg_tasks):
        t.cancel()
    if cache_swr._bg_tasks:
        await asyncio.gather(*list(cache_swr._bg_tasks), return_exceptions=True)
    cache_swr._bg_tasks.clear()
    await close_redis()


@pytest.fixture(autouse=True)
async def _fresh_redis():
    await _reset()
    yield
    await _reset()


async def _drain_background() -> None:
    """Let any spawned background refresh tasks finish."""
    for _ in range(5):
        await asyncio.sleep(0)
        if cache_swr._bg_tasks:
            await asyncio.gather(*list(cache_swr._bg_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_cold_miss_refreshes_and_caches() -> None:
    calls = {"n": 0}

    async def refresh() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    key = "test:swr:cold"
    out = await swr_get_or_refresh(key, fresh_ttl=60, stale_ttl=600, refresh=refresh)
    assert out == {"v": 1}
    # Second call within the fresh window must NOT refresh.
    out2 = await swr_get_or_refresh(key, fresh_ttl=60, stale_ttl=600, refresh=refresh)
    assert out2 == {"v": 1}
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_stale_serves_immediately_and_refreshes_in_background() -> None:
    calls = {"n": 0}

    async def refresh() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    key = "test:swr:stale"
    # fresh_ttl=0 → the stored value is stale on the very next read.
    first = await swr_get_or_refresh(key, fresh_ttl=0, stale_ttl=600, refresh=refresh)
    assert first == {"v": 1}

    # Next read returns the stale value instantly and kicks a background refresh.
    second = await swr_get_or_refresh(key, fresh_ttl=0, stale_ttl=600, refresh=refresh)
    assert second == {"v": 1}  # served stale
    await _drain_background()
    assert calls["n"] == 2  # background refresh ran


@pytest.mark.asyncio
async def test_budget_gate_vetoes_background_refresh() -> None:
    calls = {"n": 0}

    async def refresh() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    async def deny() -> bool:
        return False

    key = "test:swr:veto"
    await swr_get_or_refresh(key, fresh_ttl=0, stale_ttl=600, refresh=refresh)
    assert calls["n"] == 1
    # Stale read with a denying gate must serve stale and NOT refresh.
    out = await swr_get_or_refresh(
        key, fresh_ttl=0, stale_ttl=600, refresh=refresh, should_refresh=deny
    )
    assert out == {"v": 1}
    await _drain_background()
    assert calls["n"] == 1  # vetoed — no extra upstream call


@pytest.mark.asyncio
async def test_concurrent_cold_miss_runs_refresh_once() -> None:
    # Two concurrent cold misses on the same key: the lock holder runs the (slow)
    # refresh once and the other waits for that result instead of duplicating it.
    calls = {"n": 0}

    async def slow_refresh() -> dict:
        calls["n"] += 1
        await asyncio.sleep(0.3)  # simulate a slow catalog sync
        return {"v": calls["n"]}

    # Warm the client so both concurrent callers share ONE store — we're testing
    # the single-flight lock, not the client-init race.
    from app.platform.redis_client import get_redis

    await get_redis()

    key = "test:swr:concurrent"
    a, b = await asyncio.gather(
        swr_get_or_refresh(key, fresh_ttl=60, stale_ttl=600, refresh=slow_refresh),
        swr_get_or_refresh(key, fresh_ttl=60, stale_ttl=600, refresh=slow_refresh),
    )
    assert a == b == {"v": 1}
    assert calls["n"] == 1  # single-flight: only one sync ran


@pytest.mark.asyncio
async def test_single_flight_lock_blocks_concurrent_refresh() -> None:
    # With the lock already held, a background (require_lock) refresh no-ops.
    from app.platform.redis_client import get_redis

    key = "test:swr:lock"
    r = await get_redis()
    await r.set(f"{key}:lock", "1", ex=60, nx=True)  # pre-hold the lock

    calls = {"n": 0}

    async def refresh() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    # Seed a stale value directly so the read takes the background path.
    await cache_swr._store(r, key, {"v": 0}, fresh_ttl=0, stale_ttl=600)
    out = await swr_get_or_refresh(key, fresh_ttl=0, stale_ttl=600, refresh=refresh)
    assert out == {"v": 0}  # stale served
    await _drain_background()
    assert calls["n"] == 0  # lock held by a peer → no duplicate refresh
