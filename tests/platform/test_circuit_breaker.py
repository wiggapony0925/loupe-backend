"""Unit tests for the in-process circuit breaker."""

from __future__ import annotations

import asyncio

import pytest

from app.platform.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    get_breaker,
    reset_all_breakers,
)


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()


@pytest.mark.asyncio
async def test_breaker_passes_through_when_closed() -> None:
    breaker = CircuitBreaker(name="ok", failure_threshold=3, cooldown_s=10.0)

    async with breaker.guard():
        pass

    assert breaker.failures == 0
    assert breaker.opened_at is None


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_failures() -> None:
    breaker = CircuitBreaker(name="flaky", failure_threshold=3, cooldown_s=10.0)

    for _ in range(3):
        with pytest.raises(RuntimeError):
            async with breaker.guard():
                raise RuntimeError("upstream boom")

    # 4th call short-circuits without invoking the body.
    called = False
    with pytest.raises(CircuitOpenError) as excinfo:
        async with breaker.guard():
            called = True
    assert called is False
    assert excinfo.value.name == "flaky"
    assert excinfo.value.retry_after_s > 0


@pytest.mark.asyncio
async def test_breaker_half_open_probe_recovers_on_success(monkeypatch) -> None:
    breaker = CircuitBreaker(name="recover", failure_threshold=2, cooldown_s=10.0)

    # Trip it open.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            async with breaker.guard():
                raise RuntimeError("boom")
    assert breaker.opened_at is not None

    # Fast-forward past the cooldown.
    breaker.opened_at = breaker.opened_at - 100.0  # 100s in the past

    # Half-open probe succeeds → breaker closes.
    async with breaker.guard():
        pass

    assert breaker.failures == 0
    assert breaker.opened_at is None


@pytest.mark.asyncio
async def test_breaker_half_open_probe_reopens_on_failure() -> None:
    breaker = CircuitBreaker(name="still-bad", failure_threshold=2, cooldown_s=10.0)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            async with breaker.guard():
                raise RuntimeError("boom")

    original_opened_at = breaker.opened_at
    assert original_opened_at is not None

    # Cooldown elapsed → half-open.
    breaker.opened_at = original_opened_at - 100.0

    # Probe fails → breaker re-opens with a fresh cooldown clock.
    with pytest.raises(RuntimeError):
        async with breaker.guard():
            raise RuntimeError("still down")

    assert breaker.opened_at is not None
    assert breaker.opened_at > original_opened_at - 100.0


@pytest.mark.asyncio
async def test_concurrent_callers_only_one_probe() -> None:
    """While a probe is in flight, other callers should see CircuitOpenError."""
    breaker = CircuitBreaker(name="thundering", failure_threshold=1, cooldown_s=10.0)

    with pytest.raises(RuntimeError):
        async with breaker.guard():
            raise RuntimeError("boom")

    # Force half-open.
    assert breaker.opened_at is not None
    breaker.opened_at = breaker.opened_at - 100.0

    probe_started = asyncio.Event()
    probe_release = asyncio.Event()

    async def slow_probe() -> None:
        async with breaker.guard():
            probe_started.set()
            await probe_release.wait()

    probe_task = asyncio.create_task(slow_probe())
    await probe_started.wait()

    # Second concurrent caller hits the probe lock → fast-fails.
    with pytest.raises(CircuitOpenError):
        async with breaker.guard():
            pass

    probe_release.set()
    await probe_task


def test_get_breaker_returns_same_instance() -> None:
    a = get_breaker("shared")
    b = get_breaker("shared")
    assert a is b
