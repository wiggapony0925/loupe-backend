"""App-layer ownership policy contract (stand-in for DB RLS).

This project does **not** use Postgres row-level security. Every sensitive
query filters by ``user_id`` in the service layer. These tests pin that
behaviour for grades + collection membership so a missing ownership check
cannot ship unnoticed.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _hold(client, headers, db_session, *, name: str, **extra) -> str:
    card = await make_card(db_session, name=name)
    body = {"card_id": str(card.id), "grade": "9.0", "house": "psa", **extra}
    r = await client.post("/v1/grades", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _collection(client, headers, name: str) -> str:
    data = assert_envelope_ok(
        await client.post("/v1/collections", headers=headers, json={"name": name}),
        expected_status=201,
    )
    return data["id"]


@pytest.mark.asyncio
async def test_cannot_read_another_users_grade(
    client, auth_headers, second_user_headers, db_session
):
    gid = await _hold(client, auth_headers, db_session, name="Mine")
    # Other user listing vault must not see it.
    vault = assert_envelope_ok(
        await client.get("/v1/grades", headers=second_user_headers)
    )
    assert gid not in {row["id"] for row in vault}
    # Direct PATCH / DELETE are 404 (not 403) to avoid id enumeration.
    r = await client.patch(
        f"/v1/grades/{gid}",
        headers=second_user_headers,
        json={"notes": "hacked"},
    )
    assert r.status_code in (403, 404), r.text
    r = await client.delete(f"/v1/grades/{gid}", headers=second_user_headers)
    assert r.status_code in (403, 404), r.text


@pytest.mark.asyncio
async def test_cannot_add_foreign_holding_to_my_collection(
    client, auth_headers, second_user_headers, db_session
):
    foreign_gid = await _hold(client, second_user_headers, db_session, name="Theirs")
    my_cid = await _collection(client, auth_headers, "PC")
    r = await client.post(
        f"/v1/collections/{my_cid}/items/bulk",
        headers=auth_headers,
        json={"graded_card_ids": [foreign_gid]},
    )
    assert r.status_code == 200, r.text
    body = assert_envelope_ok(r)
    # Ownership filter drops the foreign id — zero rows added.
    assert body["added"] == 0
    items = assert_envelope_ok(
        await client.get(f"/v1/collections/{my_cid}/items", headers=auth_headers)
    )
    assert items == []


@pytest.mark.asyncio
async def test_cannot_mutate_another_users_collection(
    client, auth_headers, second_user_headers, db_session
):
    theirs = await _collection(client, second_user_headers, "Secret")
    my_gid = await _hold(client, auth_headers, db_session, name="Mine")
    for path, payload in (
        (f"/v1/collections/{theirs}/items/bulk", {"graded_card_ids": [my_gid]}),
        (f"/v1/collections/{theirs}/items/bulk-remove", {"graded_card_ids": [my_gid]}),
    ):
        r = await client.post(path, headers=auth_headers, json=payload)
        assert r.status_code in (403, 404), (path, r.text)
