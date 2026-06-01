"""Tests for the resilient HTTP helper that wraps third-party clients."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations._http import _resilient
from app.platform.circuit_breaker import CircuitOpenError, reset_all_breakers


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Install an httpx mock transport that runs `handler(request)`."""

    transport = httpx.MockTransport(handler)
    real_init = httpx.AsyncClient.__init__

    def fake_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)


@pytest.mark.asyncio
async def test_request_json_returns_body_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json={"ok": True}))

    body = await _resilient.request_json(
        integration="test-200",
        method="GET",
        url="https://example.com/x",
        timeout_s=1.0,
    )
    assert body == {"ok": True}


@pytest.mark.asyncio
async def test_request_json_404_with_not_found_ok_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

    body = await _resilient.request_json(
        integration="test-404",
        method="GET",
        url="https://example.com/missing",
        timeout_s=1.0,
        not_found_ok=True,
    )
    assert body is None


@pytest.mark.asyncio
async def test_request_json_extra_ok_status_returns_empty_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, lambda req: httpx.Response(400))

    body = await _resilient.request_json(
        integration="test-400",
        method="GET",
        url="https://example.com/x",
        timeout_s=1.0,
        extra_ok_statuses=(400,),
    )
    assert body == {"data": []}


@pytest.mark.asyncio
async def test_request_json_404_without_not_found_ok_trips_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

    integration = "test-404-raises"
    # Threshold = 2 so two 404s trip the breaker.
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await _resilient.request_json(
                integration=integration,
                method="GET",
                url="https://example.com/x",
                timeout_s=1.0,
                breaker_threshold=2,
            )

    with pytest.raises(CircuitOpenError):
        await _resilient.request_json(
            integration=integration,
            method="GET",
            url="https://example.com/x",
            timeout_s=1.0,
            breaker_threshold=2,
        )


@pytest.mark.asyncio
async def test_expected_404_does_not_trip_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expected 404 (not_found_ok) must NOT count toward the breaker."""
    _patch_httpx(monkeypatch, lambda req: httpx.Response(404))

    integration = "test-clean-misses"
    for _ in range(10):
        body = await _resilient.request_json(
            integration=integration,
            method="GET",
            url="https://example.com/missing",
            timeout_s=1.0,
            not_found_ok=True,
            breaker_threshold=3,
        )
        assert body is None
    # No CircuitOpenError after 10 clean misses.


@pytest.mark.asyncio
async def test_5xx_trips_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(monkeypatch, lambda req: httpx.Response(503))

    integration = "test-5xx"
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await _resilient.request_json(
                integration=integration,
                method="GET",
                url="https://example.com/x",
                timeout_s=1.0,
                breaker_threshold=2,
            )

    with pytest.raises(CircuitOpenError):
        await _resilient.request_json(
            integration=integration,
            method="GET",
            url="https://example.com/x",
            timeout_s=1.0,
            breaker_threshold=2,
        )
