"""Tests for /v1/analytics/overview — server-side Analytics tab rollup."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card, make_card_with_price_history


@pytest.mark.asyncio
async def test_analytics_overview_requires_auth(client):
    res = await client.get("/v1/analytics/overview")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_analytics_overview_empty_for_new_user(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    assert body["stats"]["holdings"] == 0
    assert body["stats"]["totalValueUsd"] == 0.0
    assert body["setIndexes"] == []
    assert body["movers"] == {"gainers": [], "losers": []}
    assert body["concentration"] is None
    assert body["yearDistribution"] == []
    # Grade distribution renders zero-filled buckets so the UI keeps layout.
    assert all(b["count"] == 0 for b in body["gradeDistribution"])


@pytest.mark.asyncio
async def test_analytics_overview_computes_stats_and_concentration(
    client, auth_headers, db_session, created_user
):
    card_a = await make_card(db_session, name="Charizard", year=1999)
    card_b = await make_card(db_session, name="Blastoise", year=2003)
    card_c = await make_card(db_session, name="Pikachu", year=2010)
    now = datetime.now(UTC)
    for card, value, grade in [
        (card_a, Decimal("900.00"), Decimal("9.5")),
        (card_b, Decimal("100.00"), Decimal("9.0")),
        (card_c, Decimal("50.00"), Decimal("8.0")),
    ]:
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=grade,
                house=GradeHouseEnum.psa,
                estimated_value_usd=value,
                graded_at=now,
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    stats = body["stats"]
    assert stats["holdings"] == 3
    assert stats["totalValueUsd"] == 1050.0
    assert stats["oldestYear"] == 1999
    # gem-rate counts grades >= 9.5 → 1 of 3
    assert stats["gemRatePct"] == pytest.approx(33.33, abs=0.1)

    conc = body["concentration"]
    # Top-1 is Charizard at 900/1050 ≈ 85.7%
    assert conc["top1"]["cardName"] == "Charizard"
    assert conc["top1Pct"] == pytest.approx(85.71, abs=0.1)
    assert conc["top3Pct"] == pytest.approx(100.0, abs=0.1)

    # All scans were PSA → grader split reflects it.
    assert body["kpis"]["graderSplit"] == {"psa": 3, "bgs": 0, "cgc": 0}


@pytest.mark.asyncio
async def test_analytics_overview_movers_split_gainers_and_losers(
    client, auth_headers, db_session, created_user
):
    today = date.today()
    one_year_ago = today - timedelta(days=365)
    up = await make_card_with_price_history(
        db_session, [(one_year_ago, 100.0), (today, 200.0)], name="Up"
    )
    down = await make_card_with_price_history(
        db_session, [(one_year_ago, 200.0), (today, 100.0)], name="Down"
    )
    flat_no_history = await make_card(db_session, name="Flat")
    now = datetime.now(UTC)
    for c in (up, down, flat_no_history):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=c.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("100.00"),
                graded_at=now,
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    gainers = body["movers"]["gainers"]
    losers = body["movers"]["losers"]
    assert [g["cardName"] for g in gainers] == ["Up"]
    assert [m["cardName"] for m in losers] == ["Down"]
    # Card without price history is excluded from movers entirely.
    assert "Flat" not in [m["cardName"] for m in gainers + losers]


@pytest.mark.asyncio
async def test_analytics_overview_year_distribution_buckets_by_decade(
    client, auth_headers, db_session, created_user
):
    c1 = await make_card(db_session, year=1999, name="A")
    c2 = await make_card(db_session, year=2001, name="B")
    c3 = await make_card(db_session, year=2009, name="C")
    now = datetime.now(UTC)
    for card in (c1, c2, c3):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("50.00"),
                graded_at=now,
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    decades = {b["decade"]: b["count"] for b in body["yearDistribution"]}
    assert decades == {1990: 1, 2000: 2}
