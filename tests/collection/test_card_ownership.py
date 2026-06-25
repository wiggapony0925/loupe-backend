"""Per-card ownership endpoint — GET /v1/cards/{id}/ownership."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.enums import AcquisitionSourceEnum, GradeHouseEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card


@pytest.mark.asyncio
async def test_ownership_composes_holdings_and_rollups(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.psa,  # graded
                estimated_value_usd=Decimal("250.00"),
                purchase_price_usd=Decimal("100.00"),
                purchase_date=date.today() - timedelta(days=30),
                acquired_via=AcquisitionSourceEnum.scan,
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("8.0"),
                house=GradeHouseEnum.loupe,  # raw → not graded
                estimated_value_usd=Decimal("50.00"),
                purchase_price_usd=Decimal("60.00"),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(f"/v1/cards/{card.id}/ownership", headers=auth_headers)
    body = assert_envelope_ok(resp)

    assert body["owned"] is True
    assert body["copies"] == 2
    assert len(body["holdings"]) == 2
    # Roll-ups: cost 160, value 300, P/L +140 (+87.5%).
    assert Decimal(str(body["cost_basis_usd"])) == Decimal("160.00")
    assert Decimal(str(body["holding_value_usd"])) == Decimal("300.00")
    assert Decimal(str(body["unrealized_pl_usd"])) == Decimal("140.00")
    assert round(body["unrealized_pl_pct"], 1) == 87.5
    # is_graded derived from house (psa = True, loupe = False).
    assert sorted(h["is_graded"] for h in body["holdings"]) == [False, True]
    # The graded copy carries its acquisition source + a positive days_held.
    graded = next(h for h in body["holdings"] if h["is_graded"])
    assert graded["acquired_via"] == "scan"
    assert graded["days_held"] >= 30
    assert Decimal(str(graded["unrealized_pl_usd"])) == Decimal("150.00")


@pytest.mark.asyncio
async def test_ownership_false_when_user_owns_none(client, auth_headers, db_session):
    card = await make_card(db_session)
    resp = await client.get(f"/v1/cards/{card.id}/ownership", headers=auth_headers)
    body = assert_envelope_ok(resp)
    assert body["owned"] is False
    assert body["copies"] == 0
    assert body["holdings"] == []
    assert body["unrealized_pl_usd"] is None


@pytest.mark.asyncio
async def test_ownership_requires_auth(client, db_session):
    card = await make_card(db_session)
    resp = await client.get(f"/v1/cards/{card.id}/ownership")
    assert_envelope_error(resp, expected_status=401)
