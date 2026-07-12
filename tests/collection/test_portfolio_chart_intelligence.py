"""Chart intelligence — the enrichment fields on /grades/history.

Beyond Robinhood: the history payload now carries range high/low, the
cost-basis line, best/worst day, per-holding move attribution, and
acquisition markers — all derived from the same data the curve itself
uses, so the numbers can never disagree with the line.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.services.collection import portfolio_service
from tests.factories import make_card, make_card_with_price_history


async def _mk_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"intel-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Intel",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.mark.anyio
async def test_history_attributes_the_move_to_the_right_card(db_session):
    """A riser and a flat card: movers must name the riser only."""
    user = await _mk_user(db_session)
    today = datetime.now(UTC).date()

    riser = await make_card_with_price_history(
        db_session,
        [(today - timedelta(days=6), 50.0), (today, 100.0)],  # raw doubled
        name="Rising Umbreon",
    )
    flat = await make_card_with_price_history(
        db_session,
        [(today - timedelta(days=6), 20.0), (today, 20.0)],
        name="Flat Ditto",
    )
    db_session.add_all(
        [
            GradedCard(
                user_id=user.id,
                card_id=riser.id,
                grade=Decimal("9"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("200.00"),
                purchase_price_usd=Decimal("80.00"),
            ),
            GradedCard(
                user_id=user.id,
                card_id=flat.id,
                grade=Decimal("8"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("40.00"),
            ),
        ]
    )
    await db_session.commit()

    hist = await portfolio_service.history(db_session, user, "1W")
    body = hist.to_dict()

    assert len(body["movers"]) == 1, body["movers"]
    top = body["movers"][0]
    assert top["name"] == "Rising Umbreon"
    # Ratio model: value 200, raw 50→100 ⇒ week-ago contribution 100 ⇒ +100.
    assert top["deltaUsd"] == pytest.approx(100.0)
    assert top["deltaPct"] == pytest.approx(100.0)

    # Cost basis: only the riser has a recorded purchase price.
    assert body["costBasisUsd"] == pytest.approx(80.0)

    # High/low bound the rendered series.
    values = [p["priceUsd"] for p in body["points"]]
    assert body["highUsd"] == pytest.approx(max(values))
    assert body["lowUsd"] == pytest.approx(min(values))

    # Both cards were acquired today (graded_at default) → one event group.
    assert len(body["events"]) == 1
    assert body["events"][0]["count"] == 2
    assert body["events"][0]["valueUsd"] == pytest.approx(240.0)


@pytest.mark.anyio
async def test_best_and_worst_day_come_from_the_series(db_session):
    user = await _mk_user(db_session)
    today = datetime.now(UTC).date()
    # A swing: up hard mid-week, then partially back down.
    card = await make_card_with_price_history(
        db_session,
        [
            (today - timedelta(days=6), 100.0),
            (today - timedelta(days=3), 160.0),
            (today - timedelta(days=1), 130.0),
            (today, 130.0),
        ],
        name="Swinger",
    )
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("130.00"),
        )
    )
    await db_session.commit()

    body = (await portfolio_service.history(db_session, user, "1W")).to_dict()
    assert body["bestDay"] is not None and body["bestDay"]["deltaUsd"] > 0
    assert body["worstDay"] is not None and body["worstDay"]["deltaUsd"] < 0


@pytest.mark.anyio
async def test_1d_omits_day_superlatives_and_tolerates_no_history(db_session):
    """1D has no 'days' inside it; cards without history stay un-attributed."""
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="No History")
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("50.00"),
        )
    )
    await db_session.commit()

    body = (await portfolio_service.history(db_session, user, "1D")).to_dict()
    assert body["bestDay"] is None
    assert body["worstDay"] is None
    assert body["movers"] == []
    assert body["costBasisUsd"] is None
    # The wire shape stays additive-safe for old clients.
    for key in ("range", "points", "deltaUsd", "deltaPct"):
        assert key in body
