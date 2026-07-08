"""Vault tags + the richer ``/v1/grades`` filters (multi-house / value / sort)."""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _create(client, auth_headers, db_session, **overrides) -> dict:
    card = await make_card(db_session)
    body = {"card_id": str(card.id), "grade": "9.0", "house": "loupe", **overrides}
    r = await client.post("/v1/grades", headers=auth_headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


@pytest.mark.asyncio
async def test_create_with_tags_and_filter_by_tag(client, auth_headers, db_session):
    pc = await _create(
        client,
        auth_headers,
        db_session,
        tags=["PC", "Graded"],
        estimated_value_usd="500.00",
    )
    await _create(client, auth_headers, db_session, tags=["For sale"])
    assert set(pc["tags"]) == {"PC", "Graded"}

    # Filter to a tag, case-insensitive — only the PC card.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?tags=pc", headers=auth_headers)
    )
    assert [r["id"] for r in rows] == [pc["id"]]

    # ANY-match across multiple tags returns both.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?tags=PC&tags=For+sale", headers=auth_headers)
    )
    assert len(rows) == 2

    # A tag nobody has → empty.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?tags=nope", headers=auth_headers)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_tags_validator_cleans_input(client, auth_headers, db_session):
    row = await _create(
        client,
        auth_headers,
        db_session,
        # duplicate (case), whitespace, empty, over-long.
        tags=["  PC  ", "pc", "", "Trade", "x" * 40],
    )
    tags = row["tags"]
    assert "PC" in tags  # trimmed, first casing wins
    assert sum(1 for t in tags if t.lower() == "pc") == 1  # case-insensitive de-dupe
    assert "" not in tags
    assert all(len(t) <= 24 for t in tags)  # length cap


@pytest.mark.asyncio
async def test_patch_tags_replace_and_clear(client, auth_headers, db_session):
    gid = (await _create(client, auth_headers, db_session, tags=["PC"]))["id"]

    upd = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{gid}", headers=auth_headers, json={"tags": ["Trade"]}
        )
    )
    assert upd["tags"] == ["Trade"]

    upd = assert_envelope_ok(
        await client.patch(f"/v1/grades/{gid}", headers=auth_headers, json={"tags": []})
    )
    assert upd["tags"] == []

    # Omitting tags leaves them unchanged (here: still empty).
    assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{gid}", headers=auth_headers, json={"notes": "hi"}
        )
    )
    rows = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert rows[0]["tags"] == []


@pytest.mark.asyncio
async def test_summary_exposes_available_tags(client, auth_headers, db_session):
    await _create(client, auth_headers, db_session, tags=["PC", "Trade"])
    await _create(client, auth_headers, db_session, tags=["pc", "For sale"])
    summary = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    tags = summary["availableTags"]
    assert {"PC", "Trade", "For sale"} <= set(tags)
    assert sum(1 for t in tags if t.lower() == "pc") == 1  # de-duped case-insensitively


@pytest.mark.asyncio
async def test_multi_house_value_range_and_sort(client, auth_headers, db_session):
    await _create(
        client,
        auth_headers,
        db_session,
        house="psa",
        grade="10.0",
        estimated_value_usd="1000.00",
    )
    await _create(
        client,
        auth_headers,
        db_session,
        house="bgs",
        grade="9.5",
        estimated_value_usd="300.00",
    )
    await _create(
        client,
        auth_headers,
        db_session,
        house="loupe",
        grade="8.0",
        estimated_value_usd="50.00",
    )

    # Multi-select grading houses.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?house=psa&house=bgs", headers=auth_headers)
    )
    assert len(rows) == 2

    # Value range (inclusive) — only the $300 BGS card.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?min_value=100&max_value=500", headers=auth_headers)
    )
    assert len(rows) == 1

    # Sort by value descending.
    rows = assert_envelope_ok(
        await client.get("/v1/grades?sort=value_desc", headers=auth_headers)
    )
    vals = [float(r["estimated_value_usd"]) for r in rows]
    assert vals == sorted(vals, reverse=True)
