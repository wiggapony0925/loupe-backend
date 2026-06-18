"""Tests for the resilient HTTP helper that wraps third-party clients."""

from __future__ import annotations

import httpx
import pytest

from app.integrations import base as _base
from app.integrations._http import _resilient
from app.platform.circuit_breaker import CircuitOpenError, reset_all_breakers


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Point the shared pooled client at an httpx mock transport.

    ``request_json`` now reuses the process-wide
    :func:`app.integrations.base.get_http_client` instead of constructing a
    client per call, so the mock is installed by swapping the cached client
    for one wired to a :class:`httpx.MockTransport`.
    """

    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)

    async def _get_mock_client() -> httpx.AsyncClient:
        return mock_client

    monkeypatch.setattr(_base, "get_http_client", _get_mock_client)
    monkeypatch.setattr(_resilient, "get_http_client", _get_mock_client)


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
