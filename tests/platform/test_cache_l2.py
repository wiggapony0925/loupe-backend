"""Durable L2 cache (kv_cache) — direct API + revival through the L1 wrappers."""

from __future__ import annotations

import pytest

from app.platform import cache_l2


@pytest.mark.asyncio
async def test_kv_roundtrip_and_overwrite(db_engine):
    await cache_l2.kv_set("k1", '{"a": 1}', ttl_seconds=60)
    assert await cache_l2.kv_get("k1") == '{"a": 1}'

    await cache_l2.kv_set("k1", '{"a": 2}', ttl_seconds=60)
    assert await cache_l2.kv_get("k1") == '{"a": 2}'


@pytest.mark.asyncio
async def test_kv_expiry_and_missing(db_engine):
    assert await cache_l2.kv_get("nope") is None
    await cache_l2.kv_set("gone", "x", ttl_seconds=-5)
    assert await cache_l2.kv_get("gone") is None


@pytest.mark.asyncio
async def test_kv_delete(db_engine):
    await cache_l2.kv_set("k", "v", ttl_seconds=60)
    await cache_l2.kv_delete("k")
    assert await cache_l2.kv_get("k") is None


@pytest.mark.asyncio
async def test_kv_get_survives_missing_table():
    """No table (fresh DB, migration not applied) → miss, never an exception."""
    from app.db import reset_engine

    await reset_engine()
    assert await cache_l2.kv_get("whatever") is None
    await cache_l2.kv_set("whatever", "v", ttl_seconds=30)  # must not raise
    await reset_engine()


@pytest.mark.asyncio
async def test_cache_get_revives_from_l2_after_l1_wipe(db_engine):
    """The instance-recycle scenario this tier exists for: L1 (in-process
    fallback Redis) is wiped, but the durable copy still serves — and gets
    re-seeded into the new L1."""
    from app.platform import redis_client
    from app.services.catalog.card_search_service import _cache_get, _cache_set

    await _cache_set("loupe:test:revive", {"hello": "world"}, ttl=300)

    # Simulate an instance recycle: brand-new empty in-process "Redis".
    redis_client._client = None

    got = await _cache_get("loupe:test:revive")
    assert got == {"hello": "world"}

    # Now present in the fresh L1 too.
    r = await redis_client.get_redis()
    assert await r.get("loupe:test:revive") is not None


@pytest.mark.asyncio
async def test_swr_envelope_survives_l1_wipe(db_engine):
    """cache_swr values revive from L2 with their fresh_until intact."""
    from app.platform import redis_client
    from app.platform.cache_swr import swr_get_or_refresh

    calls = {"n": 0}

    async def _refresh():
        calls["n"] += 1
        return {"cards": [calls["n"]]}

    first = await swr_get_or_refresh(
        "loupe:test:swr", fresh_ttl=3600, stale_ttl=7200, refresh=_refresh
    )
    assert first == {"cards": [1]}

    redis_client._client = None  # instance recycle

    second = await swr_get_or_refresh(
        "loupe:test:swr", fresh_ttl=3600, stale_ttl=7200, refresh=_refresh
    )
    assert second == {"cards": [1]}
    assert calls["n"] == 1  # still fresh — revived, not re-fetched
