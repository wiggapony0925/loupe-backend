"""Tests for the per-integration monthly API budget meter."""

from __future__ import annotations

import pytest

from app.platform.api_budget import ApiBudget
from app.platform.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _fresh_redis():
    # Reset the process-wide in-memory Redis stub so each test starts clean.
    await close_redis()
    yield
    await close_redis()


@pytest.mark.asyncio
async def test_spend_increments_and_used_reflects() -> None:
    b = ApiBudget("test-int", 100)
    assert await b.used() == 0
    assert await b.spend() == 1
    assert await b.spend(4) == 5
    assert await b.used() == 5
    assert await b.remaining() == 95


@pytest.mark.asyncio
async def test_can_spend_stops_at_soft_ceiling() -> None:
    # soft_ratio 0.9 → ceiling 90 of 100.
    b = ApiBudget("test-ceiling", 100, soft_ratio=0.9)
    await b.spend(89)
    assert await b.can_spend(1) is True  # 89 + 1 == 90 ≤ 90
    await b.spend(1)  # now 90
    assert await b.can_spend(1) is False  # 90 + 1 > 90
    assert await b.can_spend() is False


@pytest.mark.asyncio
async def test_zero_limit_never_blocks() -> None:
    # A 0/unknown limit must fail open so it never takes the site down.
    b = ApiBudget("test-zero", 0)
    assert await b.can_spend(10_000) is True


@pytest.mark.asyncio
async def test_usage_snapshot_shape() -> None:
    b = ApiBudget("test-usage", 1000, soft_ratio=0.9)
    await b.spend(950)
    usage = await b.usage()
    assert usage["integration"] == "test-usage"
    assert usage["used"] == 950
    assert usage["limit"] == 1000
    assert usage["remaining"] == 50
    assert usage["soft_ceiling"] == 900
    assert usage["exhausted"] is True  # 950 ≥ 900
    assert isinstance(usage["period"], str) and len(usage["period"]) == 7
