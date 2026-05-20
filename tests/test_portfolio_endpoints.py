"""Tests for /v1/grades portfolio analytics endpoints and /v1/scanners/status."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card, make_card_with_price_history, make_scanner


@pytest.mark.asyncio
async def test_summary_empty_for_new_user(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert body["totalValueUsd"] == 0
    assert body["cardCount"] == 0
    assert body["avgGrade"] is None
    assert body["avgAccuracy"] is None
    # Cost-basis fields are present but null when the user has nothing
    # — UI hides the P/L chip rather than showing "+$0.00".
    assert body["totalCostUsd"] is None
    assert body["costBasisCardCount"] == 0
    assert body["unrealizedPnlUsd"] is None
    assert body["unrealizedPnlPct"] is None


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
    # No purchase prices set => cost-basis fields stay null.
    assert body["totalCostUsd"] is None
    assert body["unrealizedPnlUsd"] is None
    assert body["unrealizedPnlPct"] is None


@pytest.mark.asyncio
async def test_summary_pnl_math_when_cost_basis_set(
    client, auth_headers, db_session, created_user
):
    """Cost basis: total value 350, total cost 200 => +150 (+75%)."""
    card = await make_card(db_session)
    seeds = [
        # (grade, estimate, purchase)
        ("9.5", "100.00", "80.00"),
        ("10.0", "250.00", "120.00"),
    ]
    for grade, val, cost in seeds:
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal(grade),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal(val),
                purchase_price_usd=Decimal(cost),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert float(body["totalCostUsd"]) == pytest.approx(200.0)
    assert body["costBasisCardCount"] == 2
    assert float(body["unrealizedPnlUsd"]) == pytest.approx(150.0)
    assert float(body["unrealizedPnlPct"]) == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_summary_pnl_only_counts_cards_with_recorded_cost(
    client, auth_headers, db_session, created_user
):
    """Mixing recorded-cost and no-cost cards: cost sum only includes the
    cards that have a purchase price. P/L is therefore an honest figure
    over the subset the user actually tracks."""
    card = await make_card(db_session)
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
                purchase_price_usd=Decimal("60.00"),
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("500.00"),  # no purchase recorded
            ),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert float(body["totalValueUsd"]) == pytest.approx(600.0)
    assert float(body["totalCostUsd"]) == pytest.approx(60.0)
    assert body["costBasisCardCount"] == 1
    # P/L is value(600) - cost(60) = 540; pct = 540/60*100 = 900
    assert float(body["unrealizedPnlUsd"]) == pytest.approx(540.0)
    assert float(body["unrealizedPnlPct"]) == pytest.approx(900.0)


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


# ---------------------------------------------------------------------------
# Real-data history tests — the contract the user explicitly cares about:
#   "the graphs should be the users collection price, the prices are adjusted
#    constantly live with the market data price of that card, and whether
#    they want to see it by day/month it should all work".
# ---------------------------------------------------------------------------


_RANGES = ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"]


def _wire_envelope_shape_ok(body: dict) -> None:
    """Frontend `PortfolioHistoryWire` contract assertion."""
    assert set(body.keys()) >= {"range", "points", "deltaUsd", "deltaPct"}
    assert isinstance(body["points"], list)
    for p in body["points"]:
        assert set(p.keys()) == {"date", "priceUsd"}
        # `date` must be ISO YYYY-MM-DD parsable
        date.fromisoformat(p["date"])
        assert isinstance(p["priceUsd"], (int, float))


async def _seed_user_with_priced_card(
    db_session, created_user, history: list[tuple[date, float]], estimate: str
):
    card = await make_card_with_price_history(db_session, history)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("9.5"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal(estimate),
        )
    )
    await db_session.commit()
    return card


@pytest.mark.asyncio
@pytest.mark.parametrize("range_", _RANGES)
async def test_history_shape_for_every_range(
    client, auth_headers, db_session, created_user, range_
):
    today = datetime.now(UTC).date()
    history = [
        (today - timedelta(days=365), 100.0),
        (today - timedelta(days=180), 120.0),
        (today - timedelta(days=30), 140.0),
        (today, 150.0),
    ]
    await _seed_user_with_priced_card(db_session, created_user, history, "150.00")

    body = assert_envelope_ok(
        await client.get(f"/v1/grades/history?range={range_}", headers=auth_headers)
    )
    assert body["range"] == range_
    _wire_envelope_shape_ok(body)
    assert len(body["points"]) >= 1, f"{range_} should produce at least one bucket"


@pytest.mark.asyncio
async def test_history_last_point_equals_sum_of_card_values(
    client, auth_headers, db_session, created_user
):
    """Proves "graphs are the user's collection price" — the last bucket
    must equal the sum of the latest per-card prices."""
    today = datetime.now(UTC).date()
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(today - timedelta(days=30), 50.0), (today, 80.0)],
        "80.00",
    )
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(today - timedelta(days=30), 200.0), (today, 220.0)],
        "220.00",
    )

    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1M", headers=auth_headers)
    )
    last = body["points"][-1]["priceUsd"]
    assert float(last) == pytest.approx(80.0 + 220.0)


@pytest.mark.asyncio
async def test_history_points_are_date_ordered(
    client, auth_headers, db_session, created_user
):
    today = datetime.now(UTC).date()
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(today - timedelta(days=90), 100.0), (today, 130.0)],
        "130.00",
    )
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=3M", headers=auth_headers)
    )
    dates = [date.fromisoformat(p["date"]) for p in body["points"]]
    assert dates == sorted(dates)


@pytest.mark.asyncio
async def test_history_delta_math_matches_first_and_last(
    client, auth_headers, db_session, created_user
):
    today = datetime.now(UTC).date()
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(today - timedelta(days=30), 100.0), (today, 150.0)],
        "150.00",
    )
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1M", headers=auth_headers)
    )
    first = body["points"][0]["priceUsd"]
    last = body["points"][-1]["priceUsd"]
    expected_delta = round(last - first, 2)
    expected_pct = (
        round((expected_delta / first * 100), 2) if first > 0 else 0.0
    )
    assert body["deltaUsd"] == pytest.approx(expected_delta)
    assert body["deltaPct"] == pytest.approx(expected_pct)


@pytest.mark.asyncio
async def test_history_reflects_per_card_price_changes(
    client, auth_headers, db_session, created_user
):
    """Proves "prices are adjusted constantly live with the market data
    price of that card" — bucket values must move when underlying card
    price history moves."""
    today = datetime.now(UTC).date()
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [
            (today - timedelta(days=30), 100.0),
            (today - timedelta(days=15), 110.0),
            (today, 200.0),  # big move on the underlying card
        ],
        "200.00",
    )
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1M", headers=auth_headers)
    )
    values = [p["priceUsd"] for p in body["points"]]
    assert max(values) >= 200.0, "latest card price must surface in the series"
    assert min(values) <= 110.0, "earlier card price must surface in the series"


@pytest.mark.asyncio
async def test_history_empty_when_user_has_no_cards(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1Y", headers=auth_headers)
    )
    assert body["points"] == []
    assert body["deltaUsd"] == 0.0
    assert body["deltaPct"] == 0.0


# ---------------------------------------------------------------------------
# Sparklines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sparklines_returns_14_points_from_real_history(
    client, auth_headers, db_session, created_user
):
    today = datetime.now(UTC).date()
    history = [(today - timedelta(days=29 - i), 100.0 + i) for i in range(30)]
    await _seed_user_with_priced_card(db_session, created_user, history, "129.00")

    body = assert_envelope_ok(
        await client.get("/v1/grades/sparklines", headers=auth_headers)
    )
    assert isinstance(body, list)
    assert len(body) == 1
    row = body[0]
    assert set(row.keys()) == {"cardId", "points", "deltaPct"}
    assert len(row["points"]) == 14
    # Series must climb (history was monotonically increasing)
    assert row["points"][-1] > row["points"][0]
    assert row["deltaPct"] > 0


@pytest.mark.asyncio
async def test_sparklines_flat_when_no_history(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("10.0"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("75.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/sparklines", headers=auth_headers)
    )
    assert len(body) == 1
    row = body[0]
    assert all(p == 75.0 for p in row["points"]), "flat at current estimate"
    assert row["deltaPct"] == 0.0


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
