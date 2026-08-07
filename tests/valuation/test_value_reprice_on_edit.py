"""Does the TOTAL move when a holding's facts change?

The derivation is correct in isolation (test_collection_value_paths.py). The
question that matters to an owner is whether the number they see is refreshed
when they change the holding — "I got it graded, why didn't my total move?"

This is the reported scenario, end to end through the real API.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.card import Card
from app.models.enums import GradeHouseEnum, RawConditionEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_ok
from tests.factories import make_card


async def _priced_card(db, market: float) -> Card:
    card = await make_card(db)
    card.card_metadata = {
        "pricing_summary": {"market": {"amount": market, "currency": "USD"}}
    }
    await db.commit()
    await db.refresh(card)
    return card


async def _total(client, headers) -> float:
    body = assert_envelope_ok(await client.get("/v1/grades/summary", headers=headers))
    return float(body["totalValueUsd"])


@pytest.mark.asyncio
async def test_quick_add_prices_the_holding_from_the_market(
    client, auth_headers, db_session
):
    """A quick-add sends no value. It must not land as $0."""
    card = await _priced_card(db_session, 100.0)
    assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "nm",
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_a_damaged_raw_card_is_discounted_in_the_total(
    client, auth_headers, db_session
):
    card = await _priced_card(db_session, 100.0)
    assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "dmg",
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_REPORTED_grading_a_damaged_card_updates_the_total(
    client, auth_headers, db_session, created_user
):
    """THE REPORTED CASE: a raw card is re-entered as a PSA 10 slab.

    A damaged raw card is held at 30% of market because of its condition.
    Once it is slabbed that condition no longer applies, so the holding is
    worth the full floor and the account total must move with it.
    """
    card = await _priced_card(db_session, 100.0)
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "dmg",
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(30.0)

    assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{created['id']}",
            headers=auth_headers,
            json={"house": "psa", "grade": 10},
        )
    )
    assert await _total(client, auth_headers) == pytest.approx(100.0), (
        "grading lifts the holding off its raw-condition discount"
    )


@pytest.mark.asyncio
async def test_downgrading_the_condition_lowers_the_total(
    client, auth_headers, db_session
):
    card = await _priced_card(db_session, 100.0)
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "nm",
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(100.0)

    assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{created['id']}",
            headers=auth_headers,
            json={"condition": "hp"},
        )
    )
    assert await _total(client, auth_headers) == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_an_owner_set_value_is_NEVER_silently_re_derived(
    client, auth_headers, db_session
):
    """The other half of the promise. If the owner typed a number, editing
    an unrelated field must not quietly replace it with a market estimate."""
    card = await _priced_card(db_session, 100.0)
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "nm",
                "estimated_value_usd": 5000,
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(5000.0)

    assert_envelope_ok(
        await client.patch(
            f"/v1/grades/{created['id']}",
            headers=auth_headers,
            json={"condition": "dmg"},
        )
    )
    assert await _total(client, auth_headers) == pytest.approx(5000.0), (
        "the owner's own number survives an unrelated edit"
    )


@pytest.mark.asyncio
async def test_deleting_a_holding_removes_its_value(client, auth_headers, db_session):
    card = await _priced_card(db_session, 100.0)
    created = assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "nm",
            },
        ),
        201,
    )
    assert await _total(client, auth_headers) == pytest.approx(100.0)
    await client.delete(f"/v1/grades/{created['id']}", headers=auth_headers)
    assert await _total(client, auth_headers) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_an_unpriced_card_contributes_nothing_but_still_counts(
    client, auth_headers, db_session, created_user
):
    """ "Unknown" must not read as "$0 worth" — the card is still owned."""
    card = await make_card(db_session)  # no pricing_summary at all
    assert_envelope_ok(
        await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "card_id": str(card.id),
                "grade": 0,
                "house": "loupe",
                "condition": "nm",
            },
        ),
        201,
    )
    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert body["cardCount"] == 1
    assert float(body["totalValueUsd"]) == pytest.approx(0.0)
