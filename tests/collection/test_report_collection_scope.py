"""Collection-scoped statements — `/v1/reports` with `collection_id`.

A statement can be scoped to a single collection (portfolio). Scoped and
whole-vault statements for the same window are distinct rows; the scoped
PDF covers only holdings in that collection (same `holdings_scope` seam
the dashboard/vault/analytics use).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from tests.collection.test_vault_flow_scenarios import (
    create_collection,
    create_holding,
)
from tests.conftest import assert_envelope_ok

_YEAR = datetime.now(UTC).year


@pytest.mark.asyncio
async def test_scoped_and_whole_vault_statements_coexist(
    client, auth_headers, db_session
):
    """Same window, two scopes → two rows; scoped title carries the name."""
    h1 = await create_holding(client, auth_headers, db_session, grade="9")
    await create_holding(client, auth_headers, db_session, grade="8")
    col = await create_collection(client, auth_headers, "Binder A")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{col['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h1["id"]]},
        )
    )

    whole = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "yearly", "year": _YEAR},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    scoped = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "yearly", "year": _YEAR, "collection_id": col["id"]},
            headers=auth_headers,
        ),
        expected_status=201,
    )

    assert whole["id"] != scoped["id"]
    assert whole["collection_id"] is None
    assert whole["collection_name"] is None
    assert scoped["collection_id"] == col["id"]
    assert scoped["collection_name"] == "Binder A"
    assert "Binder A" in scoped["title"]
    assert scoped["status"] == "ready"
    assert whole["status"] == "ready"

    # Both PDFs are real files.
    for rid in (whole["id"], scoped["id"]):
        resp = await client.get(f"/v1/reports/{rid}/file", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.content.startswith(b"%PDF-")

    # The list surfaces the scope so clients can chip the rows.
    rows = assert_envelope_ok(await client.get("/v1/reports", headers=auth_headers))
    by_id = {r["id"]: r for r in rows}
    assert by_id[scoped["id"]]["collection_name"] == "Binder A"
    assert by_id[whole["id"]]["collection_name"] is None


@pytest.mark.asyncio
async def test_scoped_snapshot_counts_only_collection_holdings(
    client, auth_headers, db_session, created_user
):
    """The aggregator scopes card_count/value to the collection membership."""
    from datetime import date

    from app.services.analytics.reports.aggregator import build_snapshot

    # graded_at is stored in UTC — use the UTC calendar date so the window
    # includes rows created "tomorrow" relative to a western local clock.
    today = datetime.now(UTC).date()

    h1 = await create_holding(client, auth_headers, db_session, grade="9")
    await create_holding(client, auth_headers, db_session, grade="8")
    await create_holding(client, auth_headers, db_session, grade="7")
    col = await create_collection(client, auth_headers, "Scoped")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{col['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h1["id"]]},
        )
    )

    # End the fixture session's stale snapshot so it sees the rows the
    # HTTP layer committed on its own session.
    await db_session.commit()

    start = date(today.year, 1, 1)
    whole = await build_snapshot(
        db_session,
        user=created_user,
        period_label="test",
        period_start=start,
        period_end=today,
    )
    scoped = await build_snapshot(
        db_session,
        user=created_user,
        period_label="test",
        period_start=start,
        period_end=today,
        collection_id=uuid.UUID(col["id"]),
        collection_name="Scoped",
    )
    assert whole.card_count == 3
    assert scoped.card_count == 1
    assert scoped.collection_name == "Scoped"


@pytest.mark.asyncio
async def test_scoped_generation_is_idempotent_per_scope(
    client, auth_headers, db_session
):
    h = await create_holding(client, auth_headers, db_session, grade="10")
    col = await create_collection(client, auth_headers, "Idem")
    assert_envelope_ok(
        await client.post(
            f"/v1/collections/{col['id']}/items/bulk",
            headers=auth_headers,
            json={"graded_card_ids": [h["id"]]},
        )
    )
    body = {"period": "yearly", "year": _YEAR, "collection_id": col["id"]}
    first = assert_envelope_ok(
        await client.post("/v1/reports", json=body, headers=auth_headers),
        expected_status=201,
    )
    second = assert_envelope_ok(
        await client.post("/v1/reports", json=body, headers=auth_headers),
        expected_status=201,
    )
    assert first["id"] == second["id"]


@pytest.mark.asyncio
async def test_foreign_or_unknown_collection_rejected(client, auth_headers):
    r = await client.post(
        "/v1/reports",
        json={
            "period": "yearly",
            "year": _YEAR,
            "collection_id": str(uuid.uuid4()),
        },
        headers=auth_headers,
    )
    assert r.status_code == 404
