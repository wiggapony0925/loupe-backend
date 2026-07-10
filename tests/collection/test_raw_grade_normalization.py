"""RAW ↔ graded normalization + collection bulk membership."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum, RawConditionEnum
from app.schemas.grade import GradedCardCreate, GradedCardUpdate


def test_raw_create_forces_grade_zero_and_default_condition():
    body = GradedCardCreate(
        upstream_id="pokemontcg:base1-4",
        grade=Decimal("9.5"),
        house=GradeHouseEnum.loupe,
        condition=None,
    )
    assert body.grade == Decimal("0")
    assert body.condition == RawConditionEnum.nm
    assert body.subgrades is None


def test_raw_create_keeps_explicit_condition():
    body = GradedCardCreate(
        upstream_id="pokemontcg:base1-4",
        grade=Decimal("0"),
        house=GradeHouseEnum.loupe,
        condition=RawConditionEnum.lp,
    )
    assert body.condition == RawConditionEnum.lp
    assert body.grade == Decimal("0")


def test_slab_create_clears_condition():
    body = GradedCardCreate(
        upstream_id="pokemontcg:base1-4",
        grade=Decimal("10"),
        house=GradeHouseEnum.psa,
        condition=RawConditionEnum.nm,
    )
    assert body.condition is None
    assert body.grade == Decimal("10")


def test_update_to_raw_locks_grade():
    body = GradedCardUpdate(house=GradeHouseEnum.loupe, grade=Decimal("8"))
    assert body.grade == Decimal("0")
    assert body.condition == RawConditionEnum.nm


def test_update_to_slab_clears_condition():
    body = GradedCardUpdate(house=GradeHouseEnum.bgs, condition=RawConditionEnum.hp)
    assert body.condition is None


@pytest.mark.parametrize(
    "house",
    [
        GradeHouseEnum.psa,
        GradeHouseEnum.bgs,
        GradeHouseEnum.cgc,
        GradeHouseEnum.sgc,
        GradeHouseEnum.tag,
    ],
)
def test_every_slab_house_clears_condition(house: GradeHouseEnum):
    body = GradedCardCreate(
        upstream_id="pokemontcg:base1-4",
        grade=Decimal("9"),
        house=house,
        condition=RawConditionEnum.dmg,
    )
    assert body.condition is None
