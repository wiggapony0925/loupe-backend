"""Collection *behaviour* rules:

* deleting a collection drops the categorization, never the cards (they stay in
  the vault / "All");
* the overview drives the portfolio switcher — synthetic undeletable "All" plus
  each collection with counts + value;
* merging folds one collection into another (de-duped) and deletes the source,
  holdings untouched.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _hold(client, auth_headers, db_session, *, name: str) -> str:
    card = await make_card(db_session, name=name)
    r = await client.post(
        "/v1/grades",
        headers=auth_headers,
        json={"card_id": str(card.id), "grade": "9.0", "house": "loupe"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


async def _collection(client, auth_headers, name: str) -> str:
    coll = assert_envelope_ok(
        await client.post("/v1/collections", headers=auth_headers, json={"name": name}),
        expected_status=201,
    )
    return coll["id"]


async def _add(client, auth_headers, cid: str, graded_card_id: str) -> None:
    r = await client.post(
        f"/v1/collections/{cid}/items",
        headers=auth_headers,
        json={"graded_card_id": graded_card_id},
    )
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_deleting_a_collection_keeps_the_cards(client, auth_headers, db_session):
    gid = await _hold(client, auth_headers, db_session, name="Keeper")
    cid = await _collection(client, auth_headers, "Temp")
    await _add(client, auth_headers, cid, gid)

    r = await client.delete(f"/v1/collections/{cid}", headers=auth_headers)
    assert r.status_code == 204

    # The card is still in the vault — only the categorization was removed.
    vault = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert [x["id"] for x in vault] == [gid]


@pytest.mark.asyncio
async def test_overview_has_undeletable_all_plus_counts(
    client, auth_headers, db_session
):
    a = await _hold(client, auth_headers, db_session, name="A")
    await _hold(client, auth_headers, db_session, name="B")
    cid = await _collection(client, auth_headers, "Pokémon")
    await _add(client, auth_headers, cid, a)

    rows = assert_envelope_ok(
        await client.get("/v1/collections/overview", headers=auth_headers)
    )
    all_entry = rows[0]
    assert all_entry["is_all"] is True
    assert all_entry["id"] is None
    assert all_entry["deletable"] is False
    assert all_entry["card_count"] == 2

    poke = next(r for r in rows if r["id"] == cid)
    assert poke["card_count"] == 1
    assert poke["deletable"] is True


@pytest.mark.asyncio
async def test_merge_folds_source_into_target(client, auth_headers, db_session):
    a = await _hold(client, auth_headers, db_session, name="A")
    b = await _hold(client, auth_headers, db_session, name="B")
    target = await _collection(client, auth_headers, "Keep")
    source = await _collection(client, auth_headers, "Fold")
    await _add(client, auth_headers, target, a)
    await _add(client, auth_headers, source, b)

    r = await client.post(
        f"/v1/collections/{target}/merge",
        headers=auth_headers,
        json={"source_id": source},
    )
    assert r.status_code == 204, r.text

    # Source is gone; target now holds both; cards untouched.
    remaining = assert_envelope_ok(
        await client.get("/v1/collections", headers=auth_headers)
    )
    assert [c["id"] for c in remaining] == [target]
    items = assert_envelope_ok(
        await client.get(f"/v1/collections/{target}/items", headers=auth_headers)
    )
    assert {x["id"] for x in items} == {a, b}
    vault = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert len(vault) == 2


@pytest.mark.asyncio
async def test_merge_into_self_rejected(client, auth_headers, db_session):
    cid = await _collection(client, auth_headers, "Solo")
    r = await client.post(
        f"/v1/collections/{cid}/merge",
        headers=auth_headers,
        json={"source_id": cid},
    )
    assert r.status_code == 400
