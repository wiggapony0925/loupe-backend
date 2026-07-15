"""Loupe AI health — the automatic kill switch.

A failing model must make the FEATURE disappear (config `aiSearch.enabled`
false, `ai_search` short-circuits) instead of showing broken states; quota
failures cool down far longer than transient ones; recovery is TTL-based.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.platform.cache_l2 import kv_get
from app.services.ai import health, providers, search


@pytest.mark.asyncio
async def test_available_requires_a_configured_provider(db_engine, monkeypatch):
    settings = providers.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert await health.available() is False
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    assert await health.available() is True


@pytest.mark.asyncio
async def test_failure_cools_down_and_quota_cools_longer(db_engine, monkeypatch):
    monkeypatch.setattr(providers.get_settings(), "openai_api_key", "test-key")
    recorded: list[int] = []

    from app.services.ai import health as health_mod

    real_kv_set = health_mod.kv_set

    async def spy_kv_set(key: str, value: str, ttl: int) -> None:
        recorded.append(ttl)
        await real_kv_set(key, value, ttl)

    monkeypatch.setattr(health_mod, "kv_set", spy_kv_set)

    await health.record_failure(RuntimeError("connection timed out"))
    assert await health.available() is False
    assert recorded[-1] == health.TRANSIENT_COOLDOWN_SECONDS
    assert await kv_get("ai_search:disabled:v1") == "transient"

    await health.record_failure(RuntimeError("Error 429: insufficient_quota"))
    assert recorded[-1] == health.QUOTA_COOLDOWN_SECONDS
    assert await kv_get("ai_search:disabled:v1") == "quota"

    await health.reset()
    assert await health.available() is True


@pytest.mark.asyncio
async def test_model_failure_flips_the_config_switch(
    client, db_engine, monkeypatch
) -> None:
    # End to end: a model blowup during a search cools the feature down, and
    # the very next /app/config tells every client to hide it.
    monkeypatch.setattr(providers.get_settings(), "openai_api_key", "test-key")

    async def boom(system: str, user: str) -> str:
        raise RuntimeError("Error 429: insufficient_quota")

    monkeypatch.setattr(providers, "ask", boom)

    assert await search.ai_search("red lizard") is None  # graceful fallback

    resp = await client.get("/v1/app/config")
    body: dict[str, Any] = resp.json()["data"]
    assert body["aiSearch"]["enabled"] is False

    # And the orchestrator now short-circuits without touching the model.
    calls = {"n": 0}

    async def count(system: str, user: str) -> str:
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr(providers, "ask", count)
    assert await search.ai_search("red lizard again") is None
    assert calls["n"] == 0
