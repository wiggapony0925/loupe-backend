"""Tests for /v1/home/feed — server-rendered Home tab rails."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card, make_card_with_price_history


@pytest.mark.asyncio
async def test_home_feed_empty_for_new_user(client, auth_headers):
    body = assert_envelope_ok(await client.get("/v1/home/feed", headers=auth_headers))
    assert body == {"topMovers": [], "recentScans": []}


@pytest.mark.asyncio
async def test_home_feed_requires_auth(client):
    res = await client.get("/v1/home/feed")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_home_feed_recent_scans_orders_by_graded_at_desc(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    now = datetime.now(UTC)
    for i in range(8):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
                graded_at=now - timedelta(hours=i),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/home/feed?recentScans=6", headers=auth_headers)
    )
    assert len(body["recentScans"]) == 6
    timestamps = [r["scannedAt"] for r in body["recentScans"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_home_feed_top_movers_ranks_by_abs_change_1y(
    client, auth_headers, db_session, created_user
):
    """Cards with the largest |1y change| float to the top."""
    today = date.today()
    one_year_ago = today - timedelta(days=365)
    # Card A: doubled (+100%)
    card_up = await make_card_with_price_history(
        db_session,
        [(one_year_ago, 100.0), (today, 200.0)],
        name="Mover Up",
    )
    # Card B: halved (-50%)
    card_down = await make_card_with_price_history(
        db_session,
        [(one_year_ago, 200.0), (today, 100.0)],
        name="Mover Down",
    )
    # Card C: flat (~0%)
    card_flat = await make_card_with_price_history(
        db_session,
        [(one_year_ago, 100.0), (today, 100.5)],
        name="Mover Flat",
    )

    for c in (card_up, card_down, card_flat):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=c.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/home/feed?topMovers=3", headers=auth_headers)
    )
    movers = body["topMovers"]
    assert len(movers) == 3
    # Sort by |change_pct_1y| desc: up(100) == down(50) → up first, then down, then flat
    assert movers[0]["cardName"] == "Mover Up"
    assert movers[0]["changePct1y"] == 100.0
    assert movers[1]["cardName"] == "Mover Down"
    assert movers[1]["changePct1y"] == -50.0
    assert movers[2]["cardName"] == "Mover Flat"


@pytest.mark.asyncio
async def test_home_feed_top_movers_dedupes_by_card_id(
    client, auth_headers, db_session, created_user
):
    """Owning N copies of one card shouldn't produce N mover rows."""
    today = date.today()
    one_year_ago = today - timedelta(days=365)
    card = await make_card_with_price_history(
        db_session, [(one_year_ago, 100.0), (today, 150.0)], name="Solo"
    )
    for _ in range(4):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(await client.get("/v1/home/feed", headers=auth_headers))
    assert len(body["topMovers"]) == 1
    assert body["topMovers"][0]["cardName"] == "Solo"


@pytest.mark.asyncio
async def test_home_feed_top_movers_skips_cards_without_history(
    client, auth_headers, db_session, created_user
):
    """No price_history => no honest change_pct => row gets a null pct."""
    bare = await make_card(db_session)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=bare.id,
            grade=Decimal("9.5"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("100.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(await client.get("/v1/home/feed", headers=auth_headers))
    assert len(body["topMovers"]) == 1
    assert body["topMovers"][0]["changePct1y"] is None
