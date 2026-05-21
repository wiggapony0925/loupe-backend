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
    resp = await client.get(f"/v1/reports/{report_id}/download", headers=other_headers)
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


# ─── auto-close scheduler ────────────────────────────────────────────


def test_iter_closed_monthly_periods():
    from datetime import UTC, datetime

    from app.services.reports.scheduler import iter_closed_monthly_periods

    # Mid-May 2026 → newest closed month is April 2026.
    out = list(iter_closed_monthly_periods(datetime(2026, 5, 21, tzinfo=UTC), lookback_months=4))
    assert out == [(2026, 4), (2026, 3), (2026, 2), (2026, 1)]

    # Early January 2026 → newest closed month wraps to December 2025.
    out = list(iter_closed_monthly_periods(datetime(2026, 1, 3, tzinfo=UTC), lookback_months=3))
    assert out == [(2025, 12), (2025, 11), (2025, 10)]


def test_iter_closed_yearly_periods():
    from datetime import UTC, datetime

    from app.services.reports.scheduler import iter_closed_yearly_periods

    out = list(iter_closed_yearly_periods(datetime(2026, 5, 21, tzinfo=UTC), lookback_years=3))
    assert out == [2025, 2024, 2023]


def test_next_close_helpers():
    from datetime import UTC, datetime

    from app.services.reports.scheduler import (
        next_monthly_close,
        next_yearly_close,
    )

    now = datetime(2026, 5, 21, 14, 0, tzinfo=UTC)
    assert next_monthly_close(now) == datetime(2026, 6, 1, tzinfo=UTC)
    assert next_yearly_close(now) == datetime(2027, 1, 1, tzinfo=UTC)

    # December rollover for monthly.
    dec = datetime(2026, 12, 5, tzinfo=UTC)
    assert next_monthly_close(dec) == datetime(2027, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_upcoming_endpoint(client, auth_headers):
    """`/v1/reports/upcoming` returns the next monthly + yearly close times."""
    body = assert_envelope_ok(await client.get("/v1/reports/upcoming", headers=auth_headers))
    assert isinstance(body, list) and len(body) == 2
    kinds = {row["period"] for row in body}
    assert kinds == {"monthly", "yearly"}
    for row in body:
        assert row["closes_at"].endswith("Z") or "+00:00" in row["closes_at"]
        assert row["label"]
        assert row["period_start"] <= row["period_end"]


@pytest.mark.asyncio
async def test_run_close_cycle_skips_users_without_grades(db_session, created_user):
    """No grades → no statements, even if the user existed for the period."""
    from app.services.reports.scheduler import run_close_cycle

    counts = await run_close_cycle()
    # `created_user` exists but has no grades — nothing to materialise.
    assert counts["generated"] == 0


@pytest.mark.asyncio
async def test_run_close_cycle_materialises_past_periods(db_session, created_user):
    """A user with one backdated grade gets monthly + yearly statements."""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from sqlalchemy import select

    from app.models.enums import GradeHouseEnum
    from app.models.grade import GradedCard
    from app.models.user import User
    from app.models.user_report import UserReport
    from app.services.reports.scheduler import run_close_cycle
    from tests.factories import make_card

    # Backdate the user so they "existed" for the periods we'll close.
    long_ago = datetime.now(UTC) - timedelta(days=400)
    await db_session.execute(
        User.__table__.update().where(User.id == created_user.id).values(created_at=long_ago)
    )

    card = await make_card(db_session)
    grade = GradedCard(
        user_id=created_user.id,
        card_id=card.id,
        grade=Decimal("9.5"),
        house=GradeHouseEnum.psa,
    )
    db_session.add(grade)
    await db_session.commit()
    # Backdate the grade too — otherwise it only "exists" as of today and
    # last-month / last-year would skip it.
    await db_session.execute(
        GradedCard.__table__.update()
        .where(GradedCard.id == grade.id)
        .values(created_at=long_ago)
    )
    await db_session.commit()

    first = await run_close_cycle()
    assert first["generated"] > 0
    assert first["failed"] == 0

    # Idempotent: a second run finds everything already there.
    second = await run_close_cycle()
    assert second["generated"] == 0
    assert second["skipped"] >= first["generated"]

    # Verify rows actually landed.
    rows = (
        await db_session.execute(
            select(UserReport).where(UserReport.user_id == created_user.id)
        )
    ).scalars().all()
    assert len(rows) == first["generated"]
    assert all(r.status.value == "ready" for r in rows)
