"""Loupe AI "describe it" search — the app/services/ai package + its route.

Tests mirror the package's modules: schema validation/clamping, prompt
game-hint biasing, the orchestrator (plan cache keyed by hint, candidate
resolution, rank-preserving dedupe), and the Pro-gated route with its
graceful fallback. The model itself is always mocked at the ``providers.ask``
seam.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import ai
from app.services.ai import config as ai_config
from app.services.ai import prompts, providers, schemas, search
from app.services.catalog import card_search_service


def _card(id_: str, name: str) -> dict[str, Any]:
    return {"id": id_, "name": name, "tcg": "pokemon"}


# ── schemas: plan parsing + message clamping ──


def test_parse_plan_accepts_strict_json_and_fences() -> None:
    plan = schemas.parse_plan(
        '{"message": "Sounds like Charizard!", "game": "pokemon",'
        ' "candidates": ["Charizard", "Charmander"]}'
    )
    assert plan is not None
    assert plan.candidates == ["Charizard", "Charmander"]
    assert plan.game == "pokemon"

    fenced = schemas.parse_plan(
        'Sure! ```json\n{"message": "m", "candidates": ["Pikachu"]}\n```'
    )
    assert fenced is not None and fenced.candidates == ["Pikachu"]


def test_parse_plan_rejects_junk() -> None:
    assert schemas.parse_plan("") is None
    assert schemas.parse_plan("not json at all") is None
    # No candidates → useless plan → None.
    assert schemas.parse_plan('{"message": "m", "candidates": []}') is None
    # Unknown game is normalized to None, not an error.
    plan = schemas.parse_plan('{"message": "m", "game": "sports", "candidates": ["X"]}')
    assert plan is not None and plan.game is None


def test_message_clamped_at_word_boundary() -> None:
    # An over-chatty model degrades to a clipped message, never a dead plan.
    long_msg = ("Charizard is almost certainly what you mean because " * 20).strip()
    plan = schemas.parse_plan(
        '{"message": '
        + repr(long_msg).replace("'", '"')
        + ', "candidates": ["Charizard"]}'
    )
    assert plan is not None
    assert len(plan.message) <= ai_config.MESSAGE_MAX_CHARS
    assert plan.message.endswith("…")
    assert not plan.message[:-1].endswith(" ")  # cut on a word boundary, tidy tail


# ── prompts: the game-preference hint ──


def test_prompt_biases_toward_the_selected_game() -> None:
    base = prompts.search_system_prompt(None)
    hinted = prompts.search_system_prompt("pokemon")
    assert hinted.startswith(base)
    assert "most likely describing a Pokémon card" in hinted
    assert '"pokemon"' in hinted
    # Unhinted prompt carries no such bias.
    assert "most likely describing" not in base


# ── search: orchestration ──


def test_interleave_preserves_rank_and_dedupes() -> None:
    merged = search._interleave(
        [
            [_card("a1", "Charizard"), _card("a2", "Charizard GX")],
            [_card("a1", "Charizard"), _card("b2", "Charmander")],
        ],
        limit=10,
    )
    # Round-robin by candidate rank; the duplicate a1 appears once.
    assert [c["id"] for c in merged] == ["a1", "a2", "b2"]


@pytest.mark.asyncio
async def test_ai_search_resolves_candidates_and_caches_plan(
    db_engine, monkeypatch
) -> None:
    calls: list[str] = []

    async def fake_ask(system: str, user: str) -> str:
        calls.append(system)
        return (
            '{"message": "A red lizard with fire sounds like Charizard!",'
            ' "game": "pokemon", "candidates": ["Charizard", "Charmander"]}'
        )

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        assert tcg == "pokemon"  # the plan's game scopes the lookups
        return {"results": [_card(f"{q}-1", q), _card(f"{q}-2", q)]}

    monkeypatch.setattr(providers, "ask", fake_ask)
    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    out = await search.ai_search("red lizard with fire")
    assert out is not None
    assert out["source"] == "ai"
    assert out["message"].startswith("A red lizard")
    assert out["candidates"] == ["Charizard", "Charmander"]
    # Interleaved by candidate rank.
    assert [c["id"] for c in out["results"]] == [
        "Charizard-1",
        "Charmander-1",
        "Charizard-2",
        "Charmander-2",
    ]

    # Second ask (same folded query, different case) hits the kv cache — the
    # model is NOT called again.
    out = await search.ai_search("Red Lizard With Fire  ")
    assert out is not None and out["source"] == "ai"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_game_hint_biases_prompt_and_keys_the_cache(
    db_engine, monkeypatch
) -> None:
    systems: list[str] = []

    async def fake_ask(system: str, user: str) -> str:
        systems.append(system)
        # The model stays silent on game — the hint must drive the lookups.
        return '{"message": "Sounds like a blue dragon!", "candidates": ["Blue-Eyes"]}'

    lookups: list[str] = []

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        lookups.append(tcg)
        return {"results": [_card(f"{tcg}-{q}", q)]}

    monkeypatch.setattr(providers, "ask", fake_ask)
    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    out = await search.ai_search("blue dragon", game_hint="yugioh")
    assert out is not None
    assert systems and "Yu-Gi-Oh!" in systems[-1]  # prompt carried the bias
    assert lookups == ["yugioh"]  # hint scoped the lookups
    assert out["game"] == "yugioh"

    # The SAME question with a different hint is a different cache entry —
    # the model is asked again with the new bias.
    out = await search.ai_search("blue dragon", game_hint="pokemon")
    assert out is not None
    assert len(systems) == 2 and "Pokémon" in systems[-1]


@pytest.mark.asyncio
async def test_ai_search_none_when_unconfigured(db_engine, monkeypatch) -> None:
    settings = providers.get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert await search.ai_search("mystery card") is None


# ── the route: Pro gate + hint passthrough + fallback ──


@pytest.mark.asyncio
async def test_route_requires_auth(client):
    resp = await client.get("/v1/cards/search/ai", params={"q": "red lizard"})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_route_402_for_free_when_gating_on(
    client, created_user, auth_headers, db_session
):
    from app.models.feature_flag import FeatureFlag

    db_session.add(FeatureFlag(key="subscriptions_enabled", label="Subs", enabled=True))
    await db_session.commit()

    resp = await client.get(
        "/v1/cards/search/ai", params={"q": "red lizard"}, headers=auth_headers
    )
    assert resp.status_code == 402
    assert "ai_search_pro" in resp.text  # structured code the paywall keys on


@pytest.mark.asyncio
async def test_route_passes_the_game_hint(
    client, created_user, auth_headers, monkeypatch
):
    seen: dict[str, Any] = {}

    async def fake_ai(q: str, limit: int = 24, game_hint: str | None = None):
        seen.update({"q": q, "game_hint": game_hint})
        return {
            "query": q,
            "message": "Sounds like Charizard!",
            "candidates": ["Charizard"],
            "game": game_hint,
            "results": [_card("z1", "Charizard")],
            "total": 1,
            "source": "ai",
        }

    monkeypatch.setattr(ai, "ai_search", fake_ai)
    resp = await client.get(
        "/v1/cards/search/ai",
        params={"q": "red lizard with fire", "tcg": "pokemon"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert seen == {"q": "red lizard with fire", "game_hint": "pokemon"}
    assert resp.json()["data"]["message"] == "Sounds like Charizard!"

    # "all" (the default tag) means no preference — the hint is dropped.
    await client.get(
        "/v1/cards/search/ai",
        params={"q": "red lizard with fire", "tcg": "all"},
        headers=auth_headers,
    )
    assert seen["game_hint"] is None


@pytest.mark.asyncio
async def test_route_falls_back_to_plain_search(
    client, created_user, auth_headers, monkeypatch
):
    async def no_ai(q: str, limit: int = 24, game_hint: str | None = None):
        return None

    async def fake_search(q: str, tcg: str, limit: int) -> dict[str, Any]:
        return {"results": [_card("p1", "Pikachu")]}

    monkeypatch.setattr(ai, "ai_search", no_ai)
    monkeypatch.setattr(card_search_service, "search_cards", fake_search)

    resp = await client.get(
        "/v1/cards/search/ai", params={"q": "yellow mouse"}, headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["source"] == "fallback"
    assert body["message"] is None
    assert [c["id"] for c in body["results"]] == ["p1"]


@pytest.mark.asyncio
async def test_route_rejects_over_limit_query(client, created_user, auth_headers):
    resp = await client.get(
        "/v1/cards/search/ai",
        params={"q": "x" * (ai.QUERY_MAX_CHARS + 1)},
        headers=auth_headers,
    )
    assert resp.status_code == 422  # the client inputs enforce the same cap
