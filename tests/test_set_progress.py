"""Tests for `/v1/sets/progress` (set completion progress)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, TcgEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok


@pytest.mark.asyncio
async def test_progress_empty_for_new_user(client, auth_headers):
    body = assert_envelope_ok(
        await client.get("/v1/sets/progress", headers=auth_headers)
    )
    assert body == []


@pytest.mark.asyncio
async def test_progress_requires_auth(client):
    resp = await client.get("/v1/sets/progress")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_progress_uses_total_cards_when_available(
    client, auth_headers, db_session, created_user
):
    cset = CardSet(
        tcg=TcgEnum.pokemon, name="Test Set", code="TS1", total_cards=4
    )
    db_session.add(cset)
    await db_session.flush()
    cards = [
        Card(set_id=cset.id, tcg=TcgEnum.pokemon, name=f"C{i}", number=str(i))
        for i in range(1, 5)
    ]
    db_session.add_all(cards)
    await db_session.flush()
    # User owns 2 of 4.
    for c, val in zip(cards[:2], ["10.00", "25.00"]):
        db_session.add(
            GradedCard(
                user_id=created_user.id,
                card_id=c.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal(val),
            )
        )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/sets/progress", headers=auth_headers)
    )
    assert len(body) == 1
    row = body[0]
    assert row["setId"] == str(cset.id)
    assert row["owned"] == 2
    assert row["total"] == 4
    assert row["percent"] == 50.0
    assert row["estimatedValueUsd"] == 35.0
    # Missing-top preview returns up to 5 cards we don't yet own.
    assert len(row["missingTop"]) == 2
    missing_ids = {m["cardId"] for m in row["missingTop"]}
    assert missing_ids == {str(cards[2].id), str(cards[3].id)}


@pytest.mark.asyncio
async def test_progress_falls_back_to_indexed_card_count(
    client, auth_headers, db_session, created_user
):
    """When `CardSet.total_cards` is null we use the count of indexed
    `cards` rows as an honest upper bound rather than fabricating a
    percentage."""
    cset = CardSet(tcg=TcgEnum.pokemon, name="No Total Set", code="NTS")
    db_session.add(cset)
    await db_session.flush()
    cards = [
        Card(set_id=cset.id, tcg=TcgEnum.pokemon, name=f"X{i}", number=str(i))
        for i in range(1, 4)
    ]
    db_session.add_all(cards)
    await db_session.flush()
    db_session.add(
        GradedCard(
            user_id=created_user.id,
            card_id=cards[0].id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.loupe,
            estimated_value_usd=Decimal("5.00"),
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/sets/progress", headers=auth_headers)
    )
    assert len(body) == 1
    row = body[0]
    assert row["owned"] == 1
    assert row["total"] == 3
    assert row["percent"] == pytest.approx(33.33)
