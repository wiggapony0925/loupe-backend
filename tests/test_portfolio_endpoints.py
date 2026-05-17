"""Tests for /v1/grades portfolio analytics endpoints and /v1/scanners/status."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card, make_scanner


@pytest.mark.asyncio
async def test_summary_empty_for_new_user(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert body["totalValueUsd"] == 0
    assert body["cardCount"] == 0
    assert body["avgGrade"] is None
    assert body["avgAccuracy"] is None


@pytest.mark.asyncio
async def test_summary_aggregates_user_cards(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    for grade, val in [("9.5", "100.00"), ("10.0", "250.00")]:
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal(grade),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal(val),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert body["cardCount"] == 2
    assert float(body["totalValueUsd"]) == pytest.approx(350.0)
    assert float(body["avgGrade"]) == pytest.approx(9.75)
    assert body["avgAccuracy"] is None  # we refuse to fabricate accuracy


@pytest.mark.asyncio
async def test_history_returns_validated_range(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1M", headers=auth_headers)
    )
    assert body["range"] == "1M"
    assert isinstance(body["points"], list)
    assert "deltaUsd" in body and "deltaPct" in body


@pytest.mark.asyncio
async def test_history_rejects_unknown_range(client, auth_headers):
    resp = await client.get("/v1/grades/history?range=BOGUS", headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sparklines_shape(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/grades/sparklines", headers=auth_headers)
    )
    assert isinstance(body, list)


@pytest.mark.asyncio
async def test_scanner_status_none_when_no_scanner(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/scanners/status", headers=auth_headers)
    )
    assert body is None


@pytest.mark.asyncio
async def test_scanner_status_returns_most_recent(
    client, auth_headers, db_session, created_user
):
    await make_scanner(db_session, created_user)
    body = assert_envelope_ok(
        await client.get("/v1/scanners/status", headers=auth_headers)
    )
    assert body is not None
    assert body["name"] == "My Scanner"


@pytest.mark.asyncio
async def test_endpoints_require_auth(client):
    for path in (
        "/v1/grades/summary",
        "/v1/grades/history",
        "/v1/grades/sparklines",
        "/v1/scanners/status",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} should require auth"
