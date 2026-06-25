"""Tests for the admin grade-review queue."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, TcgEnum
from app.models.grade import GradedCard
from app.services.admin import grade_review_service


async def _seed(db, user, *, house: GradeHouseEnum, name: str) -> None:
    cs = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db.add(cs)
    await db.flush()
    card = Card(set_id=cs.id, tcg=TcgEnum.pokemon, name=name)
    db.add(card)
    await db.flush()
    db.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9.5"),
            house=house,
            subgrades={"centering": 9.5, "corners": 9},
            estimated_value_usd=Decimal("120.00"),
            graded_at=datetime.now(UTC),
        )
    )


@pytest.mark.asyncio
async def test_grade_review_defaults_to_loupe(db_session, created_user):
    await _seed(db_session, created_user, house=GradeHouseEnum.loupe, name="Pikachu")
    await _seed(db_session, created_user, house=GradeHouseEnum.psa, name="Charizard")
    await db_session.commit()

    page = await grade_review_service.list_grades(db_session)  # default loupe
    assert page.total == 1
    row = page.results[0]
    assert row.card_name == "Pikachu"
    assert row.house == "loupe"
    assert row.grade == 9.5
    assert row.subgrades == {"centering": 9.5, "corners": 9}
    assert row.estimated_value_usd == 120.0
    assert row.user_email == created_user.email
    # Both houses surface for the filter dropdown.
    assert {"loupe", "psa"} <= set(page.houses)


@pytest.mark.asyncio
async def test_grade_review_house_filter(db_session, created_user):
    await _seed(db_session, created_user, house=GradeHouseEnum.loupe, name="A")
    await _seed(db_session, created_user, house=GradeHouseEnum.psa, name="B")
    await db_session.commit()

    psa = await grade_review_service.list_grades(db_session, house="psa")
    assert psa.total == 1 and psa.results[0].house == "psa"

    everything = await grade_review_service.list_grades(db_session, house="all")
    assert everything.total == 2
