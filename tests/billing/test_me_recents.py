"""HTTP contract for `/v1/me/recents` — cross-device recent searches + views.

The client owns the merge (it dedupes and caps locally, then pushes the whole
list), so the server's job is narrow: store one row per user, hand it back
verbatim, and never trust the payload's size or shape.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok

pytestmark = pytest.mark.asyncio


def _viewed(card_id: str, name: str = "Charizard") -> dict:
    """A recently-viewed item in the client's own camelCase shape."""
    return {
        "id": card_id,
        "name": name,
        "imageUrl": f"https://img.test/{card_id}.png",
        "setName": "Base Set",
        "kind": "card",
    }


async def test_reading_recents_requires_a_token(client):
    assert_envelope_error(await client.get("/v1/me/recents"), expected_status=401)


async def test_writing_recents_requires_a_token(client):
    assert_envelope_error(
        await client.put("/v1/me/recents", json={"searches": ["charizard"]}),
        expected_status=401,
    )


async def test_a_new_user_has_empty_recents(client, auth_headers, created_user):
    """No row yet is not an error — the endpoint answers with the empty shape so
    a fresh install has nothing to special-case."""
    data = assert_envelope_ok(await client.get("/v1/me/recents", headers=auth_headers))
    assert data == {"searches": [], "viewed": []}


async def test_recents_round_trip_verbatim(client, auth_headers, created_user):
    """Viewed items are stored exactly as the client sent them (camelCase and
    all), so neither side needs a field mapping that could drift."""
    item = _viewed("card-1")
    payload = {"searches": ["charizard", "base set"], "viewed": [item]}

    written = assert_envelope_ok(
        await client.put("/v1/me/recents", json=payload, headers=auth_headers)
    )
    assert written == payload

    read_back = assert_envelope_ok(
        await client.get("/v1/me/recents", headers=auth_headers)
    )
    assert read_back == payload


async def test_a_put_replaces_the_previous_list_rather_than_merging(
    client, auth_headers, created_user
):
    """The client sends the already-merged list, so a second PUT is the whole
    truth — merging server-side would resurrect entries the user cleared."""
    await client.put(
        "/v1/me/recents",
        json={"searches": ["old"], "viewed": [_viewed("card-old")]},
        headers=auth_headers,
    )
    data = assert_envelope_ok(
        await client.put(
            "/v1/me/recents",
            json={"searches": ["new"], "viewed": []},
            headers=auth_headers,
        )
    )
    assert data == {"searches": ["new"], "viewed": []}

    read_back = assert_envelope_ok(
        await client.get("/v1/me/recents", headers=auth_headers)
    )
    assert read_back == {"searches": ["new"], "viewed": []}


async def test_recents_are_private_to_their_owner(
    client, auth_headers, created_user, second_user_headers, second_user
):
    """Search history is personal: the row is keyed by the token's user, and
    there is no path to anyone else's."""
    await client.put(
        "/v1/me/recents",
        json={"searches": ["something private"], "viewed": []},
        headers=auth_headers,
    )
    theirs = assert_envelope_ok(
        await client.get("/v1/me/recents", headers=second_user_headers)
    )
    assert theirs == {"searches": [], "viewed": []}


async def test_blank_searches_and_items_without_an_id_are_dropped(
    client, auth_headers, created_user
):
    """Defensive filtering, because the client is not a trusted validator: an
    id-less viewed entry would render as an untappable ghost row."""
    data = assert_envelope_ok(
        await client.put(
            "/v1/me/recents",
            json={
                "searches": ["pikachu", "", "   "],
                "viewed": [_viewed("card-1"), {"name": "no id here"}],
            },
            headers=auth_headers,
        )
    )
    assert data["searches"] == ["pikachu"]
    assert [v["id"] for v in data["viewed"]] == ["card-1"]


async def test_recents_are_capped_at_fifty_entries_each(
    client, auth_headers, created_user
):
    """A runaway or malicious client must not be able to grow this row without
    bound — it is read on every sign-in."""
    data = assert_envelope_ok(
        await client.put(
            "/v1/me/recents",
            json={
                "searches": [f"query-{i}" for i in range(60)],
                "viewed": [_viewed(f"card-{i}") for i in range(60)],
            },
            headers=auth_headers,
        )
    )
    assert len(data["searches"]) == 50
    assert len(data["viewed"]) == 50
    # The cap keeps the head of the list — the most recent entries.
    assert data["searches"][0] == "query-0"
    assert data["searches"][-1] == "query-49"


async def test_recents_reject_a_payload_that_is_not_a_list(
    client, auth_headers, created_user
):
    assert_envelope_error(
        await client.put(
            "/v1/me/recents",
            json={"searches": "charizard"},
            headers=auth_headers,
        ),
        expected_status=422,
    )


async def test_omitted_fields_clear_that_half_of_recents(
    client, auth_headers, created_user
):
    """Both fields default to empty, so a partial PUT is still a full replace —
    worth pinning because it reads like a PATCH otherwise."""
    await client.put(
        "/v1/me/recents",
        json={"searches": ["kept?"], "viewed": [_viewed("card-1")]},
        headers=auth_headers,
    )
    data = assert_envelope_ok(
        await client.put("/v1/me/recents", json={"viewed": []}, headers=auth_headers)
    )
    assert data == {"searches": [], "viewed": []}
