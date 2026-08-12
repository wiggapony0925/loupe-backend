"""The public place-autocomplete endpoint (`/v1/public/places/search`).

``tests/places`` pins the gazetteer *service*; this pins the HTTP contract the
profile editor talks to — that it needs no session, that a dead upstream still
answers 200 with ``degraded`` set, and where the query bounds are.
"""

from __future__ import annotations

import pytest

from app.schemas.places import PlaceSuggestion, PlaceSuggestions
from app.services.places import place_search
from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.fixture
def gazetteer(monkeypatch):
    """Stand in for the live gazetteer. The suite must never call it."""
    calls: list[str] = []
    result = PlaceSuggestions(
        places=[
            PlaceSuggestion(
                label="Berlin, Germany",
                city="Berlin",
                country="Germany",
                country_code="DE",
            )
        ]
    )

    async def fake_search(q: str) -> PlaceSuggestions:
        calls.append(q)
        return result

    monkeypatch.setattr(place_search, "search", fake_search)
    return calls


@pytest.mark.asyncio
async def test_search_returns_server_formatted_labels(client, gazetteer):
    """The label is the string the client renders AND stores, so it has to
    arrive fully formatted — the clients never assemble it themselves."""
    data = assert_envelope_ok(
        await client.get("/v1/public/places/search", params={"q": "berlin"})
    )

    assert data["places"][0]["label"] == "Berlin, Germany"
    assert data["places"][0]["country_code"] == "DE"
    assert data["degraded"] is False
    assert gazetteer == ["berlin"]


@pytest.mark.asyncio
async def test_search_needs_no_session(client, gazetteer):
    """The profile editor asks for a location before anything about the user
    matters, and place names aren't private — so this is deliberately open."""
    resp = await client.get("/v1/public/places/search", params={"q": "berlin"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_an_unreachable_gazetteer_still_answers_200(client, monkeypatch):
    """A picker that can't reach its source must not block a profile save: the
    client needs ``degraded`` so it can fall back to accepting free text."""

    async def dead(q: str) -> PlaceSuggestions:
        return PlaceSuggestions(places=[], degraded=True)

    monkeypatch.setattr(place_search, "search", dead)

    data = assert_envelope_ok(
        await client.get("/v1/public/places/search", params={"q": "berlin"})
    )
    assert data["places"] == []
    assert data["degraded"] is True


@pytest.mark.asyncio
async def test_a_missing_query_is_rejected(client):
    resp = await client.get("/v1/public/places/search")
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_an_empty_query_never_reaches_the_upstream(client, gazetteer):
    """``q=`` is a 422 at the edge rather than a wildcard lookup — the free
    gazetteer stays free by never seeing the request at all."""
    resp = await client.get("/v1/public/places/search", params={"q": ""})
    assert_envelope_error(resp, expected_status=422)
    assert gazetteer == []


@pytest.mark.asyncio
async def test_an_absurdly_long_query_is_rejected(client, gazetteer):
    resp = await client.get("/v1/public/places/search", params={"q": "x" * 81})
    assert_envelope_error(resp, expected_status=422)
    assert gazetteer == []
