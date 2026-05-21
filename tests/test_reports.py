"""Tests for `/v1/reports` — portfolio statement generation + download."""

from __future__ import annotations

from datetime import date

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok


@pytest.mark.asyncio
async def test_resolve_period_helpers():
    from app.models.enums import ReportPeriodEnum
    from app.services.reports import resolve_period

    start, end, label = resolve_period(ReportPeriodEnum.monthly, year=2024, month=2)
    assert start == date(2024, 2, 1)
    assert end == date(2024, 2, 29)  # leap year
    assert label == "February 2024"

    start, end, label = resolve_period(ReportPeriodEnum.yearly, year=2023, month=None)
    assert (start, end, label) == (date(2023, 1, 1), date(2023, 12, 31), "2023")

    with pytest.raises(ValueError):
        resolve_period(ReportPeriodEnum.monthly, year=2024, month=None)
    with pytest.raises(ValueError):
        resolve_period(ReportPeriodEnum.monthly, year=2024, month=13)


@pytest.mark.asyncio
async def test_generate_list_and_download_roundtrip(client, auth_headers):
    """POST → GET list → GET one → GET download URL, all via the live API."""
    # Generate a yearly statement for 2024 (fully in the past).
    body = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "yearly", "year": 2024},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    report_id = body["id"]
    assert body["status"] == "ready"
    assert body["period"] == "yearly"
    assert body["title"] == "2024 statement"
    assert body["file_size_bytes"] > 0
    assert body["generated_at"] is not None

    # List endpoint includes it.
    rows = assert_envelope_ok(await client.get("/v1/reports", headers=auth_headers))
    assert any(r["id"] == report_id for r in rows)

    # Single fetch.
    single = assert_envelope_ok(
        await client.get(f"/v1/reports/{report_id}", headers=auth_headers)
    )
    assert single["id"] == report_id

    # Download URL (stub backend returns a fake https URL).
    dl = assert_envelope_ok(
        await client.get(f"/v1/reports/{report_id}/download", headers=auth_headers)
    )
    assert dl["download_url"] is not None
    assert dl["expires_in_seconds"] > 0

    # Streaming fallback returns the actual PDF bytes from the stub.
    resp = await client.get(f"/v1/reports/{report_id}/file", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-")


@pytest.mark.asyncio
async def test_generate_is_idempotent_per_period(client, auth_headers):
    """Re-generating the same window returns the already-ready row, no dupes."""
    first = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "monthly", "year": 2024, "month": 3},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    second = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "monthly", "year": 2024, "month": 3},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    assert first["id"] == second["id"]


@pytest.mark.asyncio
async def test_monthly_requires_month(client, auth_headers):
    resp = await client.post(
        "/v1/reports",
        json={"period": "monthly", "year": 2024},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_future_period_rejected(client, auth_headers):
    resp = await client.post(
        "/v1/reports",
        json={"period": "yearly", "year": 2099},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_other_user_cannot_see_my_report(client, auth_headers, db_session):
    """Cross-tenant isolation: a second user gets 404 on someone else's report."""
    body = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "yearly", "year": 2024},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    report_id = body["id"]

    # Mint a second user + token.
    import uuid

    from app.auth.jwt import issue_token
    from app.models.user import User, UserSettings

    other = User(
        email=f"intruder+{uuid.uuid4().hex[:8]}@example.com",
        display_name="Intruder",
        apple_subject=f"apple-{uuid.uuid4().hex}",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(UserSettings(user_id=other.id))
    await db_session.commit()
    token, _ = issue_token(other.id, "access")
    other_headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"/v1/reports/{report_id}", headers=other_headers)
    assert_envelope_error(resp, expected_status=404)
    resp = await client.get(
        f"/v1/reports/{report_id}/download", headers=other_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_empty_vault_still_renders_pdf(client, auth_headers):
    """A user with no cards still gets a valid PDF (cover + 'empty' messaging)."""
    body = assert_envelope_ok(
        await client.post(
            "/v1/reports",
            json={"period": "yearly", "year": 2024},
            headers=auth_headers,
        ),
        expected_status=201,
    )
    assert body["status"] == "ready"
    assert body["file_size_bytes"] > 1000  # cover/copy alone is well over 1 KB
