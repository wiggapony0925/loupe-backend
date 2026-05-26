"""Smoke tests for the unauthenticated system endpoints.

``/health``, ``/version``, ``/metrics`` are intentionally exempt from the
envelope wrapper so load balancers and uptime probes can match on a stable
top-level shape.
"""

import pytest


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body
    # /health MUST NOT be wrapped in the envelope.
    assert "meta" not in body
    assert "data" not in body


@pytest.mark.asyncio
async def test_version(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"]
    assert "meta" not in body


@pytest.mark.asyncio
async def test_metrics(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "uptime_seconds" in body
    assert "meta" not in body
