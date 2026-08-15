"""The warmer has to warm the keys the clients actually ask for.

The trending cache key is

    loupe:cards:trending:{tcg}:{limit}:{rotation_stamp}

so `limit` is part of it. A warmer that runs happily on limit=24 while the
mobile home rail requests 60 warms a key nobody reads: the endpoint stays cold,
the logs look identical, and the job reports success every ten minutes. That
failure is completely silent, which is why it gets a test rather than a comment.
"""

from __future__ import annotations

import pytest

from app.tasks.cache_warm import WARM_TARGETS, warm_home_caches


def test_it_warms_the_limit_the_mobile_home_rail_requests():
    """loupe-frontend asks for limit=60 (marketRepository). If this list ever
    loses it, the home screen goes back to paying the cold price."""
    assert any(limit == 60 for _, _, limit in WARM_TARGETS), (
        "limit=60 is gone from WARM_TARGETS — the mobile home rail's cache key "
        "will never be warmed and every app launch pays the upstream cost again"
    )


def test_it_warms_the_endpoint_default_too():
    """Callers that omit `limit` get 24, and that is a different cache key."""
    assert any(limit == 24 for _, _, limit in WARM_TARGETS)


@pytest.mark.asyncio
async def test_it_calls_the_service_once_per_target(monkeypatch):
    from app.services.market import trending_service

    calls: list[tuple] = []

    async def _spy(*, tcg, sort, limit, **kw):
        calls.append((tcg, sort, limit))
        return {"items": []}

    monkeypatch.setattr(trending_service, "get_shelf", _spy)
    monkeypatch.setattr("app.tasks.cache_warm.asyncio.sleep", _noop)

    result = await warm_home_caches({})

    assert result == {"warmed": len(WARM_TARGETS), "failed": 0}
    assert set(calls) == set(WARM_TARGETS)


@pytest.mark.asyncio
async def test_one_failing_provider_does_not_abort_the_rest(monkeypatch):
    """A warmer that dies partway leaves the remaining keys cold, and a warmer
    that raises takes down every scheduled job queued behind it."""
    from app.services.market import trending_service

    seen: list[int] = []

    async def _flaky(*, tcg, sort, limit, **kw):
        seen.append(limit)
        if limit == 60:
            raise RuntimeError("upstream said no")
        return {"items": []}

    monkeypatch.setattr(trending_service, "get_shelf", _flaky)
    monkeypatch.setattr("app.tasks.cache_warm.asyncio.sleep", _noop)

    result = await warm_home_caches({})

    assert len(seen) == len(WARM_TARGETS), "it stopped at the first failure"
    assert result["failed"] == 1
    assert result["warmed"] == len(WARM_TARGETS) - 1


@pytest.mark.asyncio
async def test_it_never_raises(monkeypatch):
    from app.services.market import trending_service

    async def _always_fails(**kw):
        raise RuntimeError("everything is down")

    monkeypatch.setattr(trending_service, "get_shelf", _always_fails)
    monkeypatch.setattr("app.tasks.cache_warm.asyncio.sleep", _noop)

    result = await warm_home_caches({})
    assert result["warmed"] == 0
    assert result["failed"] == len(WARM_TARGETS)


async def _noop(*_a, **_kw):
    """Skip the politeness delay between targets so the tests stay fast."""
    return
