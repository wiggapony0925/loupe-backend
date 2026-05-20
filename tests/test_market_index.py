"""Tests for `GET /v1/market/indices/{index_id}/history`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card, make_card_with_price_history


@pytest.mark.asyncio
async def test_psa10_index_empty_when_no_cohort(client):
    body = assert_envelope_ok(
        await client.get("/v1/market/indices/psa10/history?range=1Y")
    )
    assert body["indexId"] == "psa10"
    assert body["points"] == []
    assert body["cohortSize"] == 0


@pytest.mark.asyncio
async def test_unknown_index_404(client):
    resp = await client.get("/v1/market/indices/nonsense/history?range=1Y")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_psa10_index_normalizes_to_100(
    client, db_session, created_user
):
    """Cohort of 1 card with linear price growth → last index value > 100."""
    today = datetime.now(UTC).date()
    history = [
        (today - timedelta(days=29 - i), 100.0 + i * 5.0) for i in range(30)
    ]
    card = await make_card_with_price_history(db_session, history)
    # User owns a PSA-10 graded card of this catalog card → cohort hit.
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("10.0"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("250.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/market/indices/psa10/history?range=1M")
    )
    assert body["indexId"] == "psa10"
    assert body["cohortSize"] == 1
    assert len(body["points"]) >= 2
    # First bucket normalized to 100, last bucket strictly greater (rose ~145%).
    assert body["points"][0]["indexValue"] == pytest.approx(100.0)
    assert body["points"][-1]["indexValue"] > 100.0
    assert body["deltaPct"] > 0


@pytest.mark.asyncio
async def test_psa10_index_excludes_psa9_only_cards(
    client, db_session, created_user
):
    """A card held only as PSA 9 must NOT count toward the PSA-10 cohort."""
    today = datetime.now(UTC).date()
    history = [(today - timedelta(days=i), 200.0) for i in range(10)]
    card = await make_card_with_price_history(db_session, history)
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=card.id,
            grade=Decimal("9.0"),  # < 10.0 → excluded
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("200.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/market/indices/psa10/history?range=1M")
    )
    assert body["cohortSize"] == 0
    assert body["points"] == []
