"""Tests for the /v1/admin/ai chatbot dev-tool endpoints."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any

import pytest

from app.config import get_settings
from app.services.ai import telemetry


@contextmanager
def _as_admin(email: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": "Sounds like Charizard!",
        "candidates": ["Charizard"],
        "game": "pokemon",
        "results": [],
        "total": 3,
        "source": "ai",
        "cached": True,
    }
    body.update(overrides)
    return body


@pytest.fixture
async def seeded_ask(db_session, created_user):
    ask_id = await telemetry.log_ask(
        db_session,
        user=created_user,
        query="movie promo cards",
        game_hint="pokemon",
        body=_body(),
        latency_ms=42,
    )
    assert ask_id is not None
    await telemetry.set_feedback(
        db_session, ask_id=ask_id, user=created_user, verdict="down"
    )
    return ask_id


@pytest.mark.asyncio
async def test_admin_ai_requires_admin(client, created_user, auth_headers):
    resp = await client.get("/v1/admin/ai/search/overview", headers=auth_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_ai_overview_logs_and_detail(
    client, created_user, auth_headers, seeded_ask
):
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/ai/search/overview", headers=auth_headers)
        assert resp.status_code == 200
        view = resp.json()["data"]
        assert view["asks24h"] == 1
        assert view["feedback7d"]["down"] == 1
        assert len(view["openConversations"]) == 1

        resp = await client.get(
            "/v1/admin/ai/search/logs",
            params={"feedback": "down"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        page = resp.json()["data"]
        assert page["total"] == 1
        assert page["items"][0]["query"] == "movie promo cards"

        resp = await client.get(
            f"/v1/admin/ai/search/logs/{seeded_ask}", headers=auth_headers
        )
        assert resp.status_code == 200
        detail = resp.json()["data"]
        assert detail["cacheHit"] is True
        assert detail["conversation"] == []

        resp = await client.get(
            f"/v1/admin/ai/search/logs/{uuid.uuid4()}", headers=auth_headers
        )
        assert resp.status_code == 404
