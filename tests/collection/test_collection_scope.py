"""Active-collection scoping — one collection_id scopes the vault list, the
dashboard summary, and the analytics overview identically (the reusable
`collection_service.holdings_scope` seam)."""

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
    return r.json()["data"]["id"]  # graded card id


@pytest.mark.asyncio
async def test_collection_scopes_vault_dashboard_and_analytics(
    client, auth_headers, db_session
):
    a = await _hold(client, auth_headers, db_session, name="Alpha")
    await _hold(client, auth_headers, db_session, name="Beta")

    # A collection containing only Alpha.
    coll = assert_envelope_ok(
        await client.post(
            "/v1/collections", headers=auth_headers, json={"name": "Pokémon"}
        ),
        expected_status=201,
    )
    cid = coll["id"]
    r = await client.post(
        f"/v1/collections/{cid}/items",
        headers=auth_headers,
        json={"graded_card_id": a},
    )
    assert r.status_code in (200, 201), r.text

    # Vault: whole vault = 2, scoped = 1 (Alpha only).
    allrows = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert len(allrows) == 2
    scoped = assert_envelope_ok(
        await client.get(f"/v1/grades?collection_id={cid}", headers=auth_headers)
    )
    assert [r["card_name"] for r in scoped] == ["Alpha"]

    # Analytics overview: holdings count follows the active collection.
    ov_all = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    assert ov_all["stats"]["holdings"] == 2
    ov_scoped = assert_envelope_ok(
        await client.get(
            f"/v1/analytics/overview?collection_id={cid}", headers=auth_headers
        )
    )
    assert ov_scoped["stats"]["holdings"] == 1

    # Dashboard summary is scoped too (holdings count reflects the collection).
    summ = assert_envelope_ok(
        await client.get(
            f"/v1/grades/summary?collection_id={cid}", headers=auth_headers
        )
    )
    assert summ["cardCount"] == 1


@pytest.mark.asyncio
async def test_foreign_or_unknown_collection_scopes_to_empty(
    client, auth_headers, db_session
):
    import uuid

    await _hold(client, auth_headers, db_session, name="Solo")
    rows = assert_envelope_ok(
        await client.get(
            f"/v1/grades?collection_id={uuid.uuid4()}", headers=auth_headers
        )
    )
    assert rows == []
