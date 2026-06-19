"""Public Loupe Scanner waitlist API (/v1/waitlist) — join + stats."""

from __future__ import annotations

import pytest

from app import models  # noqa: F401  -- register every table before create_all
from tests.conftest import assert_envelope_ok


@pytest.mark.asyncio
async def test_join_returns_position_and_status(client):
    body = assert_envelope_ok(
        await client.post(
            "/v1/waitlist",
            json={"email": "Ash@Example.com", "name": "Ash", "quantity": 2},
        ),
        expected_status=201,
    )
    assert body["email"] == "ash@example.com"  # normalised
    assert body["status"] == "waiting"
    assert body["position"] == 1


@pytest.mark.asyncio
async def test_join_is_idempotent_on_email(client):
    first = assert_envelope_ok(
        await client.post("/v1/waitlist", json={"email": "dup@example.com"}),
        expected_status=201,
    )
    second = assert_envelope_ok(
        await client.post(
            "/v1/waitlist", json={"email": "dup@example.com", "quantity": 3}
        ),
        expected_status=201,
    )
    assert second["id"] == first["id"]

    stats = assert_envelope_ok(await client.get("/v1/waitlist/stats"))
    assert stats["total"] == 1
    assert stats["waiting"] == 1


@pytest.mark.asyncio
async def test_stats_counts_only_waiting(client):
    for i in range(3):
        assert_envelope_ok(
            await client.post("/v1/waitlist", json={"email": f"u{i}@example.com"}),
            expected_status=201,
        )
    stats = assert_envelope_ok(await client.get("/v1/waitlist/stats"))
    assert stats["total"] == 3
    assert stats["waiting"] == 3


@pytest.mark.asyncio
async def test_join_rejects_bad_email(client):
    resp = await client.post("/v1/waitlist", json={"email": "not-an-email"})
    assert resp.status_code == 422
