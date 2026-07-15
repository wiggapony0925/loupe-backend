"""Tests for Loupe AI telemetry — ask logging, thumbs feedback, admin views."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import ai
from app.services.ai import telemetry


def _card(id_: str, name: str) -> dict[str, Any]:
    return {"id": id_, "name": name, "tcg": "pokemon"}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": "red lizard with fire",
        "message": "Sounds like Charizard!",
        "candidates": ["Charizard", "Charmeleon"],
        "game": "pokemon",
        "results": [_card("z1", "Charizard")],
        "total": 1,
        "source": "ai",
        "cached": False,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_log_ask_persists_and_feedback_round_trips(db_session, created_user):
    ask_id = await telemetry.log_ask(
        db_session,
        user=created_user,
        query="red lizard with fire",
        game_hint="pokemon",
        body=_body(),
        latency_ms=1234,
    )
    assert ask_id is not None

    detail = await telemetry.get_log(db_session, ask_id)
    assert detail is not None
    assert detail["query"] == "red lizard with fire"
    assert detail["gameHint"] == "pokemon"
    assert detail["source"] == "ai"
    assert detail["candidates"] == ["Charizard", "Charmeleon"]
    assert detail["resultCount"] == 1
    assert detail["latencyMs"] == 1234
    assert detail["feedback"] is None

    # Thumbs down, then the user changes their mind — last verdict wins.
    assert await telemetry.set_feedback(
        db_session, ask_id=ask_id, user=created_user, verdict="down"
    )
    assert await telemetry.set_feedback(
        db_session, ask_id=ask_id, user=created_user, verdict="up"
    )
    detail = await telemetry.get_log(db_session, ask_id)
    assert detail is not None and detail["feedback"] == 1
    assert detail["feedbackAt"] is not None


@pytest.mark.asyncio
async def test_feedback_rejects_foreign_or_missing_asks(db_session, created_user):
    assert not await telemetry.set_feedback(
        db_session, ask_id=uuid.uuid4(), user=created_user, verdict="up"
    )


@pytest.mark.asyncio
async def test_overview_counts_and_open_conversations(db_session, created_user):
    for i, source in enumerate(("ai", "ai", "fallback")):
        ask_id = await telemetry.log_ask(
            db_session,
            user=created_user,
            query=f"question {i}",
            game_hint=None,
            body=_body(source=source, cached=i == 1),
            latency_ms=100 * (i + 1),
        )
        assert ask_id is not None
    assert await telemetry.set_feedback(
        db_session, ask_id=ask_id, user=created_user, verdict="down"
    )

    view = await telemetry.overview(db_session)
    assert view["asks24h"] == 3
    assert view["users24h"] == 1
    assert view["feedback7d"]["down"] == 1
    assert view["feedback7d"]["satisfaction"] == 0.0
    convos = view["openConversations"]
    assert len(convos) == 1
    assert convos[0]["userEmail"] == created_user.email
    assert len(convos[0]["asks"]) == 3


@pytest.mark.asyncio
async def test_list_logs_filters(db_session, created_user):
    up_id = await telemetry.log_ask(
        db_session,
        user=created_user,
        query="movie promos",
        game_hint="pokemon",
        body=_body(),
        latency_ms=10,
    )
    await telemetry.log_ask(
        db_session,
        user=created_user,
        query="blue dragon",
        game_hint=None,
        body=_body(source="fallback", message=None, candidates=[]),
        latency_ms=20,
    )
    assert up_id is not None
    await telemetry.set_feedback(
        db_session, ask_id=up_id, user=created_user, verdict="up"
    )

    page = await telemetry.list_logs(db_session, feedback="up")
    assert page["total"] == 1 and page["items"][0]["query"] == "movie promos"
    page = await telemetry.list_logs(db_session, source="fallback")
    assert page["total"] == 1 and page["items"][0]["query"] == "blue dragon"
    page = await telemetry.list_logs(db_session, q="promo")
    assert page["total"] == 1
    page = await telemetry.list_logs(db_session, user_id=created_user.id)
    assert page["total"] == 2


@pytest.mark.asyncio
async def test_route_returns_ask_id_and_accepts_feedback(
    client, created_user, auth_headers, monkeypatch
):
    async def fake_ai(q: str, limit: int = 24, game_hint: str | None = None):
        return _body(query=q, game=game_hint)

    monkeypatch.setattr(ai, "ai_search", fake_ai)
    resp = await client.get(
        "/v1/cards/search/ai",
        params={"q": "red lizard with fire", "tcg": "pokemon"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    ask_id = resp.json()["data"]["askId"]
    assert ask_id

    resp = await client.post(
        "/v1/cards/search/ai/feedback",
        json={"askId": ask_id, "verdict": "up"},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/v1/cards/search/ai/feedback",
        json={"askId": str(uuid.uuid4()), "verdict": "down"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_feedback_route_requires_auth(client):
    resp = await client.post(
        "/v1/cards/search/ai/feedback",
        json={"askId": str(uuid.uuid4()), "verdict": "up"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_log_ask_snapshots_the_shown_cards(db_session, created_user):
    body = _body(
        results=[
            {
                "id": "pokemontcg:base1-4",
                "name": "Charizard",
                "set_name": "Base",
                "rarity": "Rare Holo",
                "images": {"large": {"url": "https://img/charizard-lg.png"}},
                "pricing_summary": {"market": {"amount": 420.5, "currency": "USD"}},
            },
            {"id": "x2", "name": "Charmeleon", "image_url": "https://img/x2.png"},
        ],
        total=2,
    )
    ask_id = await telemetry.log_ask(
        db_session,
        user=created_user,
        query="red lizard",
        game_hint=None,
        body=body,
        latency_ms=5,
    )
    assert ask_id is not None
    detail = await telemetry.get_log(db_session, ask_id)
    assert detail is not None
    shown = detail["results"]
    assert shown[0] == {
        "id": "pokemontcg:base1-4",
        "name": "Charizard",
        "setName": "Base",
        "rarity": "Rare Holo",
        "imageUrl": "https://img/charizard-lg.png",
        "price": 420.5,
    }
    assert shown[1]["imageUrl"] == "https://img/x2.png"
