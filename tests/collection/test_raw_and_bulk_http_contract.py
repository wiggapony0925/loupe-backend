"""HTTP round-trip contracts for RAW normalization + bulk membership.

Complements the Pydantic unit tests in ``test_raw_grade_normalization.py``
by proving the same rules survive POST/PATCH and land correctly in
``graded_cards`` / ``collection_items``.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import GradeHouseEnum, RawConditionEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _collection(client, headers, name: str) -> str:
    data = assert_envelope_ok(
        await client.post("/v1/collections", headers=headers, json={"name": name}),
        expected_status=201,
    )
    return data["id"]


@pytest.mark.asyncio
async def test_raw_create_persists_grade_zero_and_default_nm(
    client, auth_headers, db_session
):
    card = await make_card(db_session, name="Raw Persist")
    # Client accidentally sends a slab-looking grade with house=loupe —
    # backend must force grade=0 and default condition=nm.
    r = await client.post(
        "/v1/grades",
        headers=auth_headers,
        json={
            "card_id": str(card.id),
            "grade": "9.5",
            "house": "loupe",
            "tags": ["PC"],
        },
    )
    data = assert_envelope_ok(r, expected_status=201)
    assert data["house"] == "loupe"
    assert float(data["grade"]) == 0.0
    assert data["condition"] == "nm"
    assert data["tags"] == ["PC"]
    assert "purchase_price_usd" in data
    assert "purchase_date" in data

    row = await db_session.get(GradedCard, uuid.UUID(data["id"]))
    assert row is not None
    assert row.house == GradeHouseEnum.loupe
    assert float(row.grade) == 0.0
    assert row.condition == RawConditionEnum.nm
    assert row.tags == ["PC"]


@pytest.mark.asyncio
async def test_slab_create_clears_condition_in_db(client, auth_headers, db_session):
    card = await make_card(db_session, name="Slab Persist")
    r = await client.post(
        "/v1/grades",
        headers=auth_headers,
        json={
            "card_id": str(card.id),
            "grade": "10",
            "house": "psa",
            "condition": "nm",
        },
    )
    data = assert_envelope_ok(r, expected_status=201)
    assert data["house"] == "psa"
    assert float(data["grade"]) == 10.0
    assert data["condition"] is None

    row = await db_session.get(GradedCard, uuid.UUID(data["id"]))
    assert row is not None
    assert row.condition is None


@pytest.mark.asyncio
async def test_patch_to_raw_rewrites_grade_and_condition(
    client, auth_headers, db_session
):
    card = await make_card(db_session, name="Flip To Raw")
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={"card_id": str(card.id), "grade": "9", "house": "bgs"},
        ),
        expected_status=201,
    )
    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{created['id']}",
            headers=auth_headers,
            json={"house": "loupe", "grade": "8"},
        )
    )
    assert updated["house"] == "loupe"
    assert float(updated["grade"]) == 0.0
    assert updated["condition"] == "nm"


@pytest.mark.asyncio
async def test_bulk_add_remove_transfer_roundtrip(client, auth_headers, db_session):
    card_a = await make_card(db_session, name="Bulk A")
    card_b = await make_card(db_session, name="Bulk B")
    a = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={"card_id": str(card_a.id), "grade": "0", "house": "loupe"},
        ),
        expected_status=201,
    )["id"]
    b = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={"card_id": str(card_b.id), "grade": "0", "house": "loupe"},
        ),
        expected_status=201,
    )["id"]
    src = await _collection(client, auth_headers, "Source")
    dst = await _collection(client, auth_headers, "Dest")

    added = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{src}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a, b, a]},  # dup id is fine
        )
    )
    assert added["added"] == 2

    # Idempotent second call.
    again = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{src}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a, b]},
        )
    )
    assert again["added"] == 0

    transferred = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{dst}/items/transfer",
            headers=auth_headers,
            json={"source_id": src, "graded_card_ids": [a]},
        )
    )
    assert transferred["added"] == 1
    assert transferred["removed"] == 1

    removed = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{src}/items/bulk-remove",
            headers=auth_headers,
            json={"graded_card_ids": [b]},
        )
    )
    assert removed["removed"] == 1

    # Holdings themselves are untouched.
    vault = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert {row["id"] for row in vault} >= {a, b}

    src_items = assert_envelope_ok(
        await client.get(f"/v1/collections/{src}/items", headers=auth_headers)
    )
    dst_items = assert_envelope_ok(
        await client.get(f"/v1/collections/{dst}/items", headers=auth_headers)
    )
    assert {x["id"] for x in src_items} == set()
    assert {x["id"] for x in dst_items} == {a}
