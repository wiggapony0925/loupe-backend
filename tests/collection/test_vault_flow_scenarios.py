"""End-to-end user-flow scenarios: vault holdings × collections.

Covers the behaviours mobile/web share via the same APIs:

* quick-add RAW into All
* full form add (RAW / slab / tags / cost / collection)
* edit holding (Apply) including RAW ↔ slab flips
* multi-select organize: bulk add / remove / transfer
* collection CRUD + merge (cards stay in vault)
* overview / vault scoping / multi-copy
* cross-tenant isolation + validation edges

The matrix targets ~100 discrete assertions so regressions in how
these flows compose surface immediately.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest

from tests.conftest import assert_envelope_ok
from tests.factories import make_card

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_holding(
    client,
    headers,
    db_session,
    *,
    name: str | None = None,
    **body: Any,
) -> dict:
    card = await make_card(db_session, name=name or f"Card-{uuid.uuid4().hex[:6]}")
    payload = {
        "card_id": str(card.id),
        "grade": body.pop("grade", "0"),
        "house": body.pop("house", "loupe"),
        **body,
    }
    r = await client.post("/v1/grades", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def create_collection(client, headers, name: str, **extra: Any) -> dict:
    return assert_envelope_ok(
        await client.post(
            "/v1/collections",
            headers=headers,
            json={"name": name, **extra},
        ),
        expected_status=201,
    )


async def vault_ids(client, headers, **params) -> set[str]:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    path = "/v1/grades" + (f"?{qs}" if qs else "")
    rows = assert_envelope_ok(await client.get(path, headers=headers))
    return {row["id"] for row in rows}


async def collection_item_ids(client, headers, cid: str) -> set[str]:
    rows = assert_envelope_ok(
        await client.get(f"/v1/collections/{cid}/items", headers=headers)
    )
    return {row["id"] for row in rows}


async def overview(client, headers) -> list[dict]:
    return assert_envelope_ok(
        await client.get("/v1/collections/overview", headers=headers)
    )


# ===========================================================================
# 1. Create / quick-add scenarios (~30)
# ===========================================================================


def _slab_cases() -> list[pytest.ParameterSet]:
    """Every grading house × common grades the form can submit."""
    out: list[pytest.ParameterSet] = []
    for house in ("psa", "bgs", "cgc", "sgc", "tag"):
        for grade in ("1", "5", "7", "8", "9", "9.5", "10"):
            out.append(
                pytest.param(
                    {"grade": grade, "house": house, "condition": "nm"},
                    {
                        "grade": float(grade),
                        "house": house,
                        "condition": None,
                    },
                    id=f"slab_{house}_{grade.replace('.', '_')}",
                )
            )
    return out


CREATE_CASES: list[pytest.ParameterSet] = [
    # Quick-add RAW defaults
    pytest.param(
        {"grade": "9.5", "house": "loupe"},
        {"grade": 0.0, "house": "loupe", "condition": "nm"},
        id="quickadd_raw_forces_grade_0_nm",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe"},
        {"grade": 0.0, "house": "loupe", "condition": "nm"},
        id="raw_explicit_zero",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "condition": "lp"},
        {"grade": 0.0, "condition": "lp"},
        id="raw_lp",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "condition": "mp"},
        {"condition": "mp"},
        id="raw_mp",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "condition": "hp"},
        {"condition": "hp"},
        id="raw_hp",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "condition": "dmg"},
        {"condition": "dmg"},
        id="raw_dmg",
    ),
    # Slabs — condition cleared (full house × grade matrix)
    *_slab_cases(),
    # Optional cost / tags / notes
    pytest.param(
        {
            "grade": "0",
            "house": "loupe",
            "purchase_price_usd": "12.50",
            "purchase_date": "2024-01-15",
            "estimated_value_usd": "40",
            "notes": "bindersleeve",
            "tags": ["PC", "Chase"],
        },
        {
            "condition": "nm",
            "purchase_price_usd": "12.50",
            "purchase_date": "2024-01-15",
            "estimated_value_usd": "40.00",
            "notes": "bindersleeve",
            "tags": ["PC", "Chase"],
        },
        id="raw_full_optional_fields",
    ),
    pytest.param(
        {"grade": "10", "house": "psa", "tags": []},
        {"tags": []},
        id="slab_empty_tags",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "tags": ["  PC  ", "pc", "Grail"]},
        {"tags": ["PC", "Grail"]},  # cleaned + de-duped case-insensitively
        id="tags_clean_dedupe",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "tags": ["For Trade"]},
        {"tags": ["For Trade"]},
        id="raw_for_trade_tag",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "tags": ["Investment", "Vintage"]},
        {"tags": ["Investment", "Vintage"]},
        id="raw_investment_vintage_tags",
    ),
    pytest.param(
        {
            "grade": "10",
            "house": "psa",
            "purchase_price_usd": "200",
            "estimated_value_usd": "350",
            "notes": "black label hopeful",
        },
        {
            "house": "psa",
            "grade": 10.0,
            "purchase_price_usd": "200.00",
            "estimated_value_usd": "350.00",
            "notes": "black label hopeful",
            "condition": None,
        },
        id="slab_with_cost_and_notes",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expect", CREATE_CASES)
async def test_create_holding_scenarios(client, auth_headers, db_session, body, expect):
    data = await create_holding(client, auth_headers, db_session, **body)
    for key, want in expect.items():
        got = data[key]
        if isinstance(want, float):
            assert float(got) == pytest.approx(want), (key, got, want)
        elif key in {"purchase_price_usd", "estimated_value_usd"} and want is not None:
            assert float(got) == pytest.approx(float(want)), (key, got, want)
        else:
            assert got == want, (key, got, want)
    # Always lands in vault All.
    assert data["id"] in await vault_ids(client, auth_headers)


@pytest.mark.asyncio
async def test_create_two_copies_are_distinct_holdings(client, auth_headers, db_session):
    """Form "Copies=2" → two POSTs = two vault rows for the same card."""
    card = await make_card(db_session, name="MultiCopy")
    ids = []
    for _ in range(2):
        r = await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={"card_id": str(card.id), "grade": "0", "house": "loupe"},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["data"]["id"])
    assert ids[0] != ids[1]
    vault = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    matching = [row for row in vault if row["card_id"] == str(card.id)]
    assert len(matching) == 2
    assert matching[0].get("copies_owned") == 2


# ===========================================================================
# 2. Edit / Apply scenarios (~20)
# ===========================================================================


PATCH_CASES: list[pytest.ParameterSet] = [
    pytest.param(
        {"house": "psa", "grade": "9"},
        {"house": "loupe"},
        {"house": "loupe", "grade": 0.0, "condition": "nm"},
        id="apply_slab_to_raw",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0", "condition": "lp"},
        {"house": "psa", "grade": "10"},
        {"house": "psa", "grade": 10.0, "condition": None},
        id="apply_raw_to_slab",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0"},
        {"condition": "hp"},
        {"condition": "hp", "house": "loupe"},
        id="apply_condition_only",
    ),
    pytest.param(
        {"house": "psa", "grade": "8"},
        {"grade": "9.5"},
        {"grade": 9.5, "house": "psa"},
        id="apply_grade_bump",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0"},
        {"purchase_price_usd": "5", "purchase_date": "2023-06-01"},
        {"purchase_price_usd": "5.00", "purchase_date": "2023-06-01"},
        id="apply_cost_basis",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0", "tags": ["PC"]},
        {"tags": ["For Trade", "Investment"]},
        {"tags": ["For Trade", "Investment"]},
        id="apply_replace_tags",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0", "tags": ["PC"]},
        {"tags": []},
        {"tags": []},
        id="apply_clear_tags",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0", "notes": "old"},
        {"notes": "slab cert 123"},
        {"notes": "slab cert 123"},
        id="apply_notes",
    ),
    pytest.param(
        {"house": "loupe", "grade": "0", "estimated_value_usd": "20"},
        {"estimated_value_usd": None},
        {"estimated_value_usd": None},
        id="apply_clear_estimate",
    ),
    pytest.param(
        {"house": "bgs", "grade": "9.5"},
        {"house": "cgc", "grade": "9"},
        {"house": "cgc", "grade": 9.0, "condition": None},
        id="apply_swap_slab_house",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("create_body,patch_body,expect", PATCH_CASES)
async def test_apply_edit_scenarios(
    client, auth_headers, db_session, create_body, patch_body, expect
):
    created = await create_holding(client, auth_headers, db_session, **create_body)
    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{created['id']}",
            headers=auth_headers,
            json=patch_body,
        )
    )
    for key, want in expect.items():
        got = updated[key]
        if isinstance(want, float):
            assert float(got) == pytest.approx(want), (key, got, want)
        elif key in {"purchase_price_usd", "estimated_value_usd"} and want is not None:
            assert float(got) == pytest.approx(float(want)), (key, got, want)
        else:
            assert got == want, (key, got, want)


@pytest.mark.asyncio
async def test_delete_holding_removes_from_vault_and_collections(
    client, auth_headers, db_session
):
    h = await create_holding(client, auth_headers, db_session, name="Gone")
    coll = await create_collection(client, auth_headers, "Temp")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h["id"]]},
        )
    )
    r = await client.delete(f"/v1/grades/{h['id']}", headers=auth_headers)
    assert r.status_code == 204
    assert h["id"] not in await vault_ids(client, auth_headers)
    assert h["id"] not in await collection_item_ids(client, auth_headers, coll["id"])


# ===========================================================================
# 3. Collection CRUD (~12)
# ===========================================================================


@pytest.mark.asyncio
async def test_collection_create_list_rename_delete(client, auth_headers, db_session):
    h = await create_holding(client, auth_headers, db_session, name="Keep")
    coll = await create_collection(
        client, auth_headers, "PC", description="personal", color="#112233"
    )
    assert coll["name"] == "PC"
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items",
            headers=auth_headers,
            json={"graded_card_id": h["id"]},
        ),
        expected_status=201,
    )
    renamed = assert_envelope_ok(
        await client.patch(
            f"/v1/collections/{coll['id']}",
            headers=auth_headers,
            json={"name": "Master Set"},
        )
    )
    assert renamed["name"] == "Master Set"
    listed = assert_envelope_ok(await client.get("/v1/collections", headers=auth_headers))
    assert any(c["id"] == coll["id"] and c["name"] == "Master Set" for c in listed)

    r = await client.delete(f"/v1/collections/{coll['id']}", headers=auth_headers)
    assert r.status_code == 204
    # Holding survived; only membership gone.
    assert h["id"] in await vault_ids(client, auth_headers)
    listed = assert_envelope_ok(await client.get("/v1/collections", headers=auth_headers))
    assert coll["id"] not in {c["id"] for c in listed}


COLLECTION_VALIDATION = [
    pytest.param({"name": ""}, 422, id="empty_name"),
    pytest.param({"name": "x" * 121}, 422, id="name_too_long"),
    pytest.param({"name": "OK", "description": "d" * 501}, 422, id="desc_too_long"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", COLLECTION_VALIDATION)
async def test_collection_create_validation(client, auth_headers, body, status):
    r = await client.post("/v1/collections", headers=auth_headers, json=body)
    assert r.status_code == status


# ===========================================================================
# 4. Membership: single + bulk add/remove/transfer (~30)
# ===========================================================================


@pytest.mark.asyncio
async def test_single_add_and_remove_item(client, auth_headers, db_session):
    h = await create_holding(client, auth_headers, db_session)
    coll = await create_collection(client, auth_headers, "Binder")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items",
            headers=auth_headers,
            json={"graded_card_id": h["id"]},
        ),
        expected_status=201,
    )
    assert await collection_item_ids(client, auth_headers, coll["id"]) == {h["id"]}
    r = await client.delete(
        f"/v1/collections/{coll['id']}/items/{h['id']}",
        headers=auth_headers,
    )
    assert r.status_code == 204
    assert await collection_item_ids(client, auth_headers, coll["id"]) == set()
    assert h["id"] in await vault_ids(client, auth_headers)


BULK_IDS_CASES = [
    pytest.param(1, 1, id="bulk_one"),
    pytest.param(3, 3, id="bulk_three"),
    pytest.param(5, 5, id="bulk_five"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("n,expected_added", BULK_IDS_CASES)
async def test_bulk_add_n_cards(
    client, auth_headers, db_session, n, expected_added
):
    holdings = [
        await create_holding(client, auth_headers, db_session, name=f"B{i}")
        for i in range(n)
    ]
    coll = await create_collection(client, auth_headers, f"Bulk{n}")
    ids = [h["id"] for h in holdings]
    result = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": ids},
        )
    )
    assert result["added"] == expected_added
    assert await collection_item_ids(client, auth_headers, coll["id"]) == set(ids)

    # Idempotent re-add.
    again = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": ids},
        )
    )
    assert again["added"] == 0


@pytest.mark.asyncio
async def test_bulk_add_dedupes_request_ids(client, auth_headers, db_session):
    h = await create_holding(client, auth_headers, db_session)
    coll = await create_collection(client, auth_headers, "Dedup")
    result = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h["id"], h["id"], h["id"]]},
        )
    )
    assert result["added"] == 1


@pytest.mark.asyncio
async def test_bulk_remove_partial(client, auth_headers, db_session):
    a = await create_holding(client, auth_headers, db_session, name="A")
    b = await create_holding(client, auth_headers, db_session, name="B")
    c = await create_holding(client, auth_headers, db_session, name="C")
    coll = await create_collection(client, auth_headers, "Partial")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"], b["id"], c["id"]]},
        )
    )
    removed = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk-remove",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"], b["id"]]},
        )
    )
    assert removed["removed"] == 2
    assert await collection_item_ids(client, auth_headers, coll["id"]) == {c["id"]}


@pytest.mark.asyncio
async def test_bulk_remove_missing_is_noop(client, auth_headers, db_session):
    h = await create_holding(client, auth_headers, db_session)
    coll = await create_collection(client, auth_headers, "Noop")
    fake = str(uuid.uuid4())
    result = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk-remove",
            headers=auth_headers,
            json={"graded_card_ids": [h["id"], fake]},
        )
    )
    assert result["removed"] == 0


@pytest.mark.asyncio
async def test_transfer_moves_membership_not_holding(client, auth_headers, db_session):
    h = await create_holding(client, auth_headers, db_session)
    src = await create_collection(client, auth_headers, "Src")
    dst = await create_collection(client, auth_headers, "Dst")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{src['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h["id"]]},
        )
    )
    transferred = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{dst['id']}/items/transfer",
            headers=auth_headers,
            json={"source_id": src["id"], "graded_card_ids": [h["id"]]},
        )
    )
    assert transferred["added"] == 1
    assert transferred["removed"] == 1
    assert await collection_item_ids(client, auth_headers, src["id"]) == set()
    assert await collection_item_ids(client, auth_headers, dst["id"]) == {h["id"]}
    assert h["id"] in await vault_ids(client, auth_headers)


@pytest.mark.asyncio
async def test_transfer_partial_selection(client, auth_headers, db_session):
    a = await create_holding(client, auth_headers, db_session, name="TA")
    b = await create_holding(client, auth_headers, db_session, name="TB")
    src = await create_collection(client, auth_headers, "Src2")
    dst = await create_collection(client, auth_headers, "Dst2")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{src['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"], b["id"]]},
        )
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{dst['id']}/items/transfer",
            headers=auth_headers,
            json={"source_id": src["id"], "graded_card_ids": [a["id"]]},
        )
    )
    assert await collection_item_ids(client, auth_headers, src["id"]) == {b["id"]}
    assert await collection_item_ids(client, auth_headers, dst["id"]) == {a["id"]}


@pytest.mark.asyncio
async def test_holding_can_live_in_multiple_collections(
    client, auth_headers, db_session
):
    h = await create_holding(client, auth_headers, db_session)
    a = await create_collection(client, auth_headers, "Alpha")
    b = await create_collection(client, auth_headers, "Beta")
    for coll in (a, b):
        assert_envelope_ok(
            await client.post(
                f"/v1/collections/{coll['id']}/items/bulk",
                headers=auth_headers,
                json={"graded_card_ids": [h["id"]]},
            )
        )
    assert await collection_item_ids(client, auth_headers, a["id"]) == {h["id"]}
    assert await collection_item_ids(client, auth_headers, b["id"]) == {h["id"]}


BULK_VALIDATION = [
    pytest.param({}, 422, id="bulk_missing_ids"),
    pytest.param({"graded_card_ids": []}, 422, id="bulk_empty_ids"),
    pytest.param(
        {"graded_card_ids": [str(uuid.uuid4()) for _ in range(201)]},
        422,
        id="bulk_over_cap",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", BULK_VALIDATION)
async def test_bulk_payload_validation(client, auth_headers, db_session, payload, status):
    coll = await create_collection(client, auth_headers, "Val")
    r = await client.post(
        f"/v1/collections/{coll['id']}/items/bulk",
        headers=auth_headers,
        json=payload,
    )
    assert r.status_code == status


# ===========================================================================
# 5. Overview + vault scoping (~10)
# ===========================================================================


@pytest.mark.asyncio
async def test_overview_all_plus_counts_and_values(client, auth_headers, db_session):
    a = await create_holding(
        client,
        auth_headers,
        db_session,
        name="OA",
        grade="0",
        house="loupe",
        estimated_value_usd="100",
    )
    b = await create_holding(
        client,
        auth_headers,
        db_session,
        name="OB",
        grade="10",
        house="psa",
        estimated_value_usd="50",
    )
    coll = await create_collection(client, auth_headers, "Valued")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"]]},
        )
    )
    rows = await overview(client, auth_headers)
    all_row = rows[0]
    assert all_row["is_all"] is True
    assert all_row["deletable"] is False
    assert all_row["card_count"] == 2
    assert all_row["total_value_usd"] == pytest.approx(150.0)
    poke = next(r for r in rows if r["id"] == coll["id"])
    assert poke["card_count"] == 1
    assert poke["total_value_usd"] == pytest.approx(100.0)
    assert poke["deletable"] is True


@pytest.mark.asyncio
async def test_vault_scoped_to_collection(client, auth_headers, db_session):
    a = await create_holding(client, auth_headers, db_session, name="In")
    await create_holding(client, auth_headers, db_session, name="Out")
    coll = await create_collection(client, auth_headers, "Scope")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"]]},
        )
    )
    scoped = await vault_ids(client, auth_headers, collection_id=coll["id"])
    assert scoped == {a["id"]}
    all_ids = await vault_ids(client, auth_headers)
    assert len(all_ids) == 2


# ===========================================================================
# 6. Merge (~4)
# ===========================================================================


@pytest.mark.asyncio
async def test_merge_collections_keeps_cards(client, auth_headers, db_session):
    a = await create_holding(client, auth_headers, db_session, name="MA")
    b = await create_holding(client, auth_headers, db_session, name="MB")
    target = await create_collection(client, auth_headers, "Keep")
    source = await create_collection(client, auth_headers, "Fold")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{target['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [a["id"]]},
        )
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{source['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [b["id"]]},
        )
    )
    r = await client.post(
        f"/v1/collections/{target['id']}/merge",
        headers=auth_headers,
        json={"source_id": source["id"]},
    )
    assert r.status_code == 204, r.text
    assert await collection_item_ids(client, auth_headers, target["id"]) == {
        a["id"],
        b["id"],
    }
    listed = assert_envelope_ok(await client.get("/v1/collections", headers=auth_headers))
    assert source["id"] not in {c["id"] for c in listed}
    assert {a["id"], b["id"]} <= await vault_ids(client, auth_headers)


@pytest.mark.asyncio
async def test_merge_into_self_rejected(client, auth_headers):
    coll = await create_collection(client, auth_headers, "Solo")
    r = await client.post(
        f"/v1/collections/{coll['id']}/merge",
        headers=auth_headers,
        json={"source_id": coll["id"]},
    )
    assert r.status_code in (400, 409, 422)


# ===========================================================================
# 7. Filters / tags that users hit from vault UI (~8)
# ===========================================================================


@pytest.mark.asyncio
async def test_filter_raw_vs_graded(client, auth_headers, db_session):
    raw = await create_holding(
        client, auth_headers, db_session, name="RawF", house="loupe", grade="0"
    )
    slab = await create_holding(
        client, auth_headers, db_session, name="SlabF", house="psa", grade="10"
    )
    raw_ids = await vault_ids(client, auth_headers, raw_only="true")
    graded_ids = await vault_ids(client, auth_headers, graded_only="true")
    assert raw["id"] in raw_ids and slab["id"] not in raw_ids
    assert slab["id"] in graded_ids and raw["id"] not in graded_ids


@pytest.mark.asyncio
async def test_filter_by_tag(client, auth_headers, db_session):
    tagged = await create_holding(
        client,
        auth_headers,
        db_session,
        name="Tagged",
        tags=["Grail"],
    )
    await create_holding(client, auth_headers, db_session, name="Plain", tags=["PC"])
    ids = await vault_ids(client, auth_headers, tags="Grail")
    assert ids == {tagged["id"]}


# ===========================================================================
# 8. Cross-tenant + auth (~10)
# ===========================================================================


@pytest.mark.asyncio
async def test_other_user_cannot_see_or_organize_my_stuff(
    client, auth_headers, second_user_headers, db_session
):
    mine = await create_holding(client, auth_headers, db_session, name="Private")
    my_coll = await create_collection(client, auth_headers, "MineOnly")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{my_coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [mine["id"]]},
        )
    )

    their_vault = await vault_ids(client, second_user_headers)
    assert mine["id"] not in their_vault

    # Foreign collection mutations
    for path, payload in (
        (
            f"/v1/collections/{my_coll['id']}/items/bulk",
            {"graded_card_ids": [mine["id"]]},
        ),
        (
            f"/v1/collections/{my_coll['id']}/items/bulk-remove",
            {"graded_card_ids": [mine["id"]]},
        ),
        (
            f"/v1/collections/{my_coll['id']}",
            None,
        ),
    ):
        if payload is None:
            r = await client.delete(path, headers=second_user_headers)
        else:
            r = await client.post(path, headers=second_user_headers, json=payload)
        assert r.status_code in (403, 404), (path, r.status_code, r.text)

    # Cannot add their holding into my collection either (drop quietly).
    theirs = await create_holding(
        client, second_user_headers, db_session, name="Theirs"
    )
    result = assert_envelope_ok(
        await client.post(
            f"/v1/collections/{my_coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [theirs["id"]]},
        )
    )
    assert result["added"] == 0


AUTH_PATHS = [
    ("GET", "/v1/grades"),
    ("POST", "/v1/grades"),
    ("GET", "/v1/collections"),
    ("GET", "/v1/collections/overview"),
    ("POST", "/v1/collections"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", AUTH_PATHS)
async def test_vault_routes_require_auth(client, method, path):
    r = await client.request(method, path, json={} if method == "POST" else None)
    assert r.status_code in (401, 403)


# ===========================================================================
# 9. Full journeys that compose the product surface (~8 multi-assert flows)
# ===========================================================================


@pytest.mark.asyncio
async def test_journey_quickadd_then_apply_then_organize(
    client, auth_headers, db_session
):
    """Hold CTA → vault pencil Edit/Apply → multi-select Organize."""
    # 1) Quick-add RAW into All
    quick = await create_holding(
        client,
        auth_headers,
        db_session,
        name="Journey",
        grade="9",
        house="loupe",
    )
    assert float(quick["grade"]) == 0.0
    assert quick["condition"] == "nm"

    # 2) Apply: set cost + tag + bump condition
    applied = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{quick['id']}",
            headers=auth_headers,
            json={
                "condition": "lp",
                "purchase_price_usd": "25",
                "tags": ["PC"],
                "estimated_value_usd": "60",
            },
        )
    )
    assert applied["condition"] == "lp"
    assert float(applied["purchase_price_usd"]) == pytest.approx(25.0)
    assert applied["tags"] == ["PC"]

    # 3) Create two collections and organize
    pc = await create_collection(client, auth_headers, "PC Binder")
    trade = await create_collection(client, auth_headers, "For Trade")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{pc['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [quick["id"]]},
        )
    )
    # User later decides it's for trade → transfer
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{trade['id']}/items/transfer",
            headers=auth_headers,
            json={"source_id": pc["id"], "graded_card_ids": [quick["id"]]},
        )
    )
    assert await collection_item_ids(client, auth_headers, pc["id"]) == set()
    assert await collection_item_ids(client, auth_headers, trade["id"]) == {
        quick["id"]
    }
    # Still in All
    assert quick["id"] in await vault_ids(client, auth_headers)


@pytest.mark.asyncio
async def test_journey_form_add_into_collection_then_delete_collection(
    client, auth_headers, db_session
):
    """Form: pick collection on create → delete collection → card remains."""
    card = await make_card(db_session, name="FormIntoColl")
    coll = await create_collection(client, auth_headers, "Target")
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": "10",
                "house": "psa",
                "tags": ["Investment"],
            },
        ),
        expected_status=201,
    )
    # Membership via bulk (same as mobile create+collectionId)
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [created["id"]]},
        )
    )
    r = await client.delete(f"/v1/collections/{coll['id']}", headers=auth_headers)
    assert r.status_code == 204
    assert created["id"] in await vault_ids(client, auth_headers)


@pytest.mark.asyncio
async def test_journey_slab_back_from_psa_then_reorganize(
    client, auth_headers, db_session
):
    """Raw entry → later comes back slabbed → apply house → stay categorized."""
    raw = await create_holding(
        client, auth_headers, db_session, name="AwaitingSlab", house="loupe", grade="0"
    )
    coll = await create_collection(client, auth_headers, "Submissions")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{coll['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [raw["id"]]},
        )
    )
    slabbed = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{raw['id']}",
            headers=auth_headers,
            json={"house": "psa", "grade": "10", "notes": "cert 998877"},
        )
    )
    assert slabbed["house"] == "psa"
    assert float(slabbed["grade"]) == 10.0
    assert slabbed["condition"] is None
    assert slabbed["notes"] == "cert 998877"
    # Still in the collection after Apply
    assert await collection_item_ids(client, auth_headers, coll["id"]) == {raw["id"]}


@pytest.mark.asyncio
async def test_journey_multi_select_organize_then_merge(
    client, auth_headers, db_session
):
    ids = [
        (
            await create_holding(
                client, auth_headers, db_session, name=f"MS{i}", estimated_value_usd="10"
            )
        )["id"]
        for i in range(4)
    ]
    a = await create_collection(client, auth_headers, "Set A")
    b = await create_collection(client, auth_headers, "Set B")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{a['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": ids[:2]},
        )
    )
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{b['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": ids[2:]},
        )
    )
    r = await client.post(
        f"/v1/collections/{a['id']}/merge",
        headers=auth_headers,
        json={"source_id": b["id"]},
    )
    assert r.status_code == 204
    assert await collection_item_ids(client, auth_headers, a["id"]) == set(ids)
    ov = await overview(client, auth_headers)
    assert ov[0]["card_count"] == 4
    assert next(r for r in ov if r["id"] == a["id"])["card_count"] == 4


@pytest.mark.asyncio
async def test_journey_active_collection_pref_roundtrip(client, auth_headers, db_session):
    coll = await create_collection(client, auth_headers, "ActivePref")
    body = assert_envelope_ok(
        await client.patch(
            "/v1/me/settings",
            headers=auth_headers,
            json={"active_collection_id": coll["id"]},
        )
    )
    assert body["active_collection_id"] == coll["id"]
    body = assert_envelope_ok(
        await client.patch(
            "/v1/me/settings",
            headers=auth_headers,
            json={"active_collection_id": None},
        )
    )
    assert body["active_collection_id"] is None


CREATE_EDGE_CASES = [
    pytest.param({"grade": "-1", "house": "psa"}, 422, id="grade_below_zero"),
    pytest.param({"grade": "11", "house": "psa"}, 422, id="grade_above_ten"),
    pytest.param(
        {"grade": "0", "house": "loupe", "purchase_date": "2999-01-01"},
        422,
        id="future_purchase_date",
    ),
    pytest.param(
        {"grade": "0", "house": "loupe", "purchase_price_usd": "-1"},
        422,
        id="negative_purchase",
    ),
    pytest.param(
        {"house": "psa"},  # missing grade
        422,
        id="slab_missing_grade",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", CREATE_EDGE_CASES)
async def test_create_validation_edges(client, auth_headers, db_session, body, status):
    card = await make_card(db_session, name="Edge")
    payload = {"card_id": str(card.id), **body}
    r = await client.post("/v1/grades", headers=auth_headers, json=payload)
    assert r.status_code == status, r.text


@pytest.mark.asyncio
async def test_unknown_grade_and_collection_404(client, auth_headers):
    missing = str(uuid.uuid4())
    assert (await client.get(f"/v1/grades/{missing}", headers=auth_headers)).status_code in (
        404,
        403,
    )
    assert (
        await client.delete(f"/v1/collections/{missing}", headers=auth_headers)
    ).status_code in (404, 403)
    assert (
        await client.post(
            f"/v1/collections/{missing}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [missing]},
        )
    ).status_code in (404, 403)


@pytest.mark.asyncio
async def test_summary_reflects_add_edit_delete(client, auth_headers, db_session):
    h = await create_holding(
        client,
        auth_headers,
        db_session,
        estimated_value_usd="80",
        purchase_price_usd="50",
    )
    summary = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert summary["cardCount"] >= 1
    assert summary["totalValueUsd"] == pytest.approx(80.0) or summary[
        "totalValueUsd"
    ] >= 80.0

    assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{h['id']}",
            headers=auth_headers,
            json={"estimated_value_usd": "100"},
        )
    )
    summary2 = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert summary2["totalValueUsd"] >= summary["totalValueUsd"]

    await client.delete(f"/v1/grades/{h['id']}", headers=auth_headers)
    summary3 = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert summary3["cardCount"] == summary["cardCount"] - 1


# Count safeguard — keep this suite honest about coverage breadth.
def test_scenario_inventory_is_at_least_100():
    """Meta: ensure this module actually expands to ≥100 discrete scenarios."""
    import ast
    from pathlib import Path

    src = Path(__file__).read_text()
    tree = ast.parse(src)
    funcs = [
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name.startswith("test_")
        and node.name != "test_scenario_inventory_is_at_least_100"
    ]
    non_param = 0
    for node in funcs:
        is_param = any("parametrize" in ast.dump(d) for d in node.decorator_list)
        if not is_param:
            non_param += 1
    expanded = (
        len(CREATE_CASES)
        + len(PATCH_CASES)
        + len(COLLECTION_VALIDATION)
        + len(BULK_IDS_CASES)
        + len(BULK_VALIDATION)
        + len(AUTH_PATHS)
        + len(CREATE_EDGE_CASES)
        + non_param
    )
    assert expanded >= 100, f"scenario inventory shrank to {expanded}"
