"""Smoke tests for the unauthenticated system endpoints."""

import pytest


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body


@pytest.mark.asyncio
async def test_version(client):
    resp = await client.get("/version")
    assert resp.status_code == 200
    assert resp.json()["name"]


@pytest.mark.asyncio
async def test_metrics(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "uptime_seconds" in resp.json()
