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
async def test_summary_pnl_ignores_cards_without_cost_basis(
    client, auth_headers, db_session, created_user
):
    """P/L compares value vs cost over the SAME population.

    A card without a purchase price contributes to `totalValueUsd` but must
    not inflate `unrealizedPnlUsd` — its full value is not a "gain".
    """
    card = await make_card(db_session)
    seeds = [
        # (grade, estimate, purchase) — the 500.00 card has no cost basis.
        ("9.5", "100.00", "80.00"),
        ("10.0", "250.00", "120.00"),
        ("8.0", "500.00", None),
    ]
    for grade, val, cost in seeds:
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal(grade),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal(val),
                purchase_price_usd=Decimal(cost) if cost is not None else None,
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert float(body["totalValueUsd"]) == pytest.approx(850.0)
    assert float(body["totalCostUsd"]) == pytest.approx(200.0)
    assert body["costBasisCardCount"] == 2
    # 350 (value of the two cost-basis cards) - 200 cost = +150 (+75%),
    # NOT 850 - 200 = +650.
    assert float(body["unrealizedPnlUsd"]) == pytest.approx(150.0)
    assert float(body["unrealizedPnlPct"]) == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_summary_rolls_up_sealed_and_combined_value(
    client, auth_headers, db_session, created_user
):
    """The backend defines "collection value": cards + unopened sealed.

    Sealed value/cost multiply by quantity; opened boxes are excluded from
    the investment rollup; `combinedValueUsd` = cards + sealed so both
    clients render the same headline without summing holdings themselves.
    """
    from app.models.enums import SealedProductTypeEnum, TcgEnum
    from app.models.sealed import SealedHolding, SealedProduct

    card = await make_card(db_session)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("100.00"),
        )
    )
    product = SealedProduct(
        tcg=TcgEnum.pokemon,
        product_type=SealedProductTypeEnum.booster_box,
        name="Test Booster Box",
        msrp_usd=Decimal("161.64"),
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add_all(
        [
            SealedHolding(  # 2 x $200 value, 2 x $150 cost
                user_id=created_user.id,
                product_id=product.id,
                quantity=2,
                purchase_price_usd=Decimal("150.00"),
                estimated_value_usd=Decimal("200.00"),
            ),
            SealedHolding(  # opened → excluded from the rollup
                user_id=created_user.id,
                product_id=product.id,
                quantity=1,
                purchase_price_usd=Decimal("120.00"),
                estimated_value_usd=Decimal("500.00"),
                opened_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert float(body["totalValueUsd"]) == pytest.approx(100.0)
    assert float(body["sealedValueUsd"]) == pytest.approx(400.0)
    assert float(body["sealedCostUsd"]) == pytest.approx(300.0)
    assert body["sealedHoldingCount"] == 1
    assert float(body["combinedValueUsd"]) == pytest.approx(500.0)


@pytest.mark.asyncio
async def test_grade_patch_can_clear_nullable_value_fields(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    row = GradedCard(
        user_id=created_user.id,
        card_id=card.id,
        grade=Decimal("9.0"),
        house=GradeHouseEnum.loupe,
        estimated_value_usd=Decimal("125.00"),
        purchase_price_usd=Decimal("80.00"),
        purchase_date=date(2024, 1, 15),
        notes="keep clean",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    body = assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{row.id}",
            json={
                "estimated_value_usd": None,
                "purchase_price_usd": None,
                "purchase_date": None,
                "notes": None,
            },
            headers=auth_headers,
        )
    )
    assert body["estimated_value_usd"] is None
    assert body["purchase_price_usd"] is None
    assert body["purchase_date"] is None
    assert body["notes"] is None


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
    # P/L compares like-for-like: value of the tracked card (100) - its
    # cost (60) = 40; pct = 40/60*100 ≈ 66.67. The 500.00 no-cost card
    # contributes to totalValueUsd but never to P/L.
    assert float(body["unrealizedPnlUsd"]) == pytest.approx(40.0)
    assert float(body["unrealizedPnlPct"]) == pytest.approx(66.6667, rel=1e-4)


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
        # `date` is ISO — date-only for daily buckets, full datetimes for the
        # 1D range's intraday snapshot points. Both parse with
        # datetime.fromisoformat (and with `new Date()` on the clients).
        datetime.fromisoformat(p["date"])
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
async def test_collection_value_agrees_across_endpoints(
    client, auth_headers, db_session, created_user
):
    """The three collection-value surfaces must report the SAME total.

    Regression for the dashboard bug where ``/v1/grades/history`` (raw
    catalog market basis) reported ~$22k while ``/v1/analytics/overview``
    (grade-aware estimate basis) reported ~$50k for the same account.

    All three now run every holding through the one canonical
    ``holding_value_usd`` (grade-aware ``estimated_value_usd``) basis:

      * history terminal point  == summary.totalValueUsd
      * summary.totalValueUsd   == overview.stats.totalValueUsd

    The collection deliberately mixes cards whose *raw* market price and
    *graded* estimate diverge, so a regression back to the raw basis would
    fail the equality.
    """
    today = datetime.now(UTC).date()

    # Card A: raw price_history (300 -> 360) AND a raw live market of 360,
    # but the graded slab is estimated at 800. The raw numbers must NOT leak
    # into the total — only the grade-aware 800 counts.
    card_a = await make_card_with_price_history(
        db_session, [(today - timedelta(days=15), 300.0), (today, 360.0)], name="A"
    )
    card_a.card_metadata = {
        **(card_a.card_metadata or {}),
        "pricing_summary": {"market": {"amount": 360.0, "currency": "USD"}},
    }
    db_session.add(card_a)

    # Card B: no price history, no live market — estimate only.
    card_b = await make_card(db_session, name="B")

    # Card C: has price history, estimate 120.
    card_c = await make_card_with_price_history(
        db_session, [(today - timedelta(days=20), 90.0), (today, 110.0)], name="C"
    )

    for card, estimate in [(card_a, "800.00"), (card_b, "250.00"), (card_c, "120.00")]:
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal(estimate),
            )
        )
    await db_session.commit()

    expected_total = 800.0 + 250.0 + 120.0

    summary = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    overview = assert_envelope_ok(
        await client.get("/v1/analytics/overview", headers=auth_headers)
    )
    history = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1M", headers=auth_headers)
    )
    history_total = history["points"][-1]["priceUsd"]
    summary_total = summary["totalValueUsd"]
    overview_total = overview["stats"]["totalValueUsd"]

    # Each surface equals the canonical grade-aware total…
    assert float(summary_total) == pytest.approx(expected_total)
    assert float(overview_total) == pytest.approx(expected_total)
    assert float(history_total) == pytest.approx(expected_total)
    # …and therefore each other (the property the dashboard depends on).
    assert float(history_total) == pytest.approx(float(overview_total))
    assert float(summary_total) == pytest.approx(float(overview_total))


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
    expected_pct = round((expected_delta / first * 100), 2) if first > 0 else 0.0
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


@pytest.mark.asyncio
async def test_history_1d_reflects_yesterday_to_today_movement(
    client, auth_headers, db_session, created_user
):
    """1D must surface the day-over-day delta from per-card price history.

    Regression guard: the original implementation emitted ``[now, now]``
    which made the Command Center hero always read "Quiet day, +$0.00"
    regardless of what cards actually did overnight. We now bucket
    yesterday + today and rely on each card's ``price_history`` for the
    yesterday-close value (the today bucket is pinned to the live total
    so the hero matches the vault summary).
    """
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(yesterday, 100.0), (today, 150.0)],
        "150.00",
    )

    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1D", headers=auth_headers)
    )
    assert len(body["points"]) == 2, "1D must emit yesterday + today"
    assert body["points"][0]["date"] == yesterday.isoformat()
    assert body["points"][1]["date"] == today.isoformat()
    assert body["points"][0]["priceUsd"] == pytest.approx(100.0)
    assert body["points"][1]["priceUsd"] == pytest.approx(150.0)
    assert body["deltaUsd"] == pytest.approx(50.0)
    assert body["deltaPct"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_history_1d_zero_delta_when_card_unchanged(
    client, auth_headers, db_session, created_user
):
    """A truly flat day still legitimately reads as +$0.00 \u2014 the
    "Quiet day" copy is only correct when the underlying prices
    actually didn't move."""
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    await _seed_user_with_priced_card(
        db_session,
        created_user,
        [(yesterday, 100.0), (today, 100.0)],
        "100.00",
    )
    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1D", headers=auth_headers)
    )
    assert body["deltaUsd"] == pytest.approx(0.0)
    assert body["deltaPct"] == pytest.approx(0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("range_", ["1D", "1W", "1M", "3M", "YTD", "1Y", "ALL"])
async def test_history_no_price_history_reports_zero_delta(
    client, auth_headers, db_session, created_user, range_
):
    """Regression: a card with no recorded ``price_history`` must NOT
    inflate the delta, and its level must be the canonical grade-aware
    estimate \u2014 never the raw catalog market price.

    The pre-fix behaviour treated the scan-time estimate as
    "yesterday's price" for every historical bucket, producing
    nonsense deltas like ``+284%`` for accounts whose price-history
    table hadn't been backfilled yet. The honest answer is ``0`` \u2014
    "we don't have historical pricing for this card so we can't tell
    you how it moved".

    Collection value is ``holding_value_usd`` (``estimated_value_usd``),
    the same grade-aware basis the analytics overview uses. The raw
    ``pricing_summary.market`` price is grade-agnostic and would
    undervalue a slab, so the terminal bucket must equal the estimate,
    not the raw market number.
    """
    # Card has a raw catalog price (pricing_summary.market = $1500) but no
    # price_history. The raw price must NOT become the collection-value level.
    card = await make_card(db_session, name="No-History Chase Card")
    card.card_metadata = {"pricing_summary": {"market": {"amount": 1500.0}}}
    db_session.add(card)
    # Add to user's vault with the grade-aware estimate ($100) for this slab.
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("100.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get(f"/v1/grades/history?range={range_}", headers=auth_headers)
    )
    assert body["deltaUsd"] == pytest.approx(0.0), (
        f"{range_}: a card with no price_history must contribute 0 delta, "
        f"got {body['deltaUsd']}"
    )
    assert body["deltaPct"] == pytest.approx(0.0)
    # Terminal bucket is the canonical grade-aware estimate ($100), the same
    # value the analytics overview reports \u2014 NOT the raw market price ($1500).
    last_point = body["points"][-1]["priceUsd"]
    assert last_point == pytest.approx(100.0), (
        f"{range_}: today bucket must equal the canonical estimate ($100), "
        f"got ${last_point}"
    )


@pytest.mark.asyncio
async def test_history_1d_ignores_stale_price_history(
    client, auth_headers, db_session, created_user
):
    """Regression: a card whose only ``price_history`` entry is months
    old must not anchor the "yesterday" bucket. With stale carry-
    forward enabled, yesterday's bucket would use the ancient $50
    point while today's bucket uses the fresh $500 live price,
    producing a fake +900% intraday delta. The cap on carry-forward
    age forces yesterday to fall back to the live total → 0 delta.
    """
    today = datetime.now(UTC).date()
    sixty_days_ago = today - timedelta(days=60)
    card = await make_card_with_price_history(
        db_session, [(sixty_days_ago, 50.0)], name="Stale History Card"
    )
    # Live market is 10x the stale recorded point.
    card.card_metadata = {
        **(card.card_metadata or {}),
        "pricing_summary": {"market": {"amount": 500.0}},
    }
    db_session.add(card)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("500.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/history?range=1D", headers=auth_headers)
    )
    assert body["points"][-1]["priceUsd"] == pytest.approx(500.0)
    assert body["deltaUsd"] == pytest.approx(0.0), (
        f"stale (>2d) price_history must not anchor yesterday's bucket; "
        f"got points={body['points']}"
    )
    assert body["deltaPct"] == pytest.approx(0.0)


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
