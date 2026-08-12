"""Tests for `/v1/sets/{set_id}/checklist` (have + still-missing sheet)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, TcgEnum
from app.models.grade import GradedCard
from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _set_with_cards(db_session, *, numbers: list[str], total_cards: int | None):
    cset = CardSet(
        tcg=TcgEnum.pokemon,
        name=f"Checklist Set {uuid.uuid4().hex[:6]}",
        code=None,
        total_cards=total_cards,
    )
    db_session.add(cset)
    await db_session.flush()
    cards = [
        Card(set_id=cset.id, tcg=TcgEnum.pokemon, name=f"Card {n}", number=n)
        for n in numbers
    ]
    db_session.add_all(cards)
    await db_session.commit()
    return cset, cards


async def _own(db_session, user, card):
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.loupe,
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_checklist_lists_every_card_and_flags_the_owned_ones(
    client, auth_headers, db_session, created_user
):
    """The sheet is the whole set, not just the vault — a collector needs to
    see what is still missing, so unowned cards stay in the payload with
    `owned: false` rather than being filtered out."""
    cset, cards = await _set_with_cards(
        db_session, numbers=["1", "2", "3"], total_cards=3
    )
    await _own(db_session, created_user, cards[1])

    body = assert_envelope_ok(
        await client.get(f"/v1/sets/{cset.id}/checklist", headers=auth_headers)
    )
    assert body["setId"] == str(cset.id)
    assert body["setName"] == cset.name
    assert body["total"] == 3
    assert body["owned"] == 1
    assert len(body["cards"]) == 3
    owned = {c["name"]: c["owned"] for c in body["cards"]}
    assert owned == {"Card 1": False, "Card 2": True, "Card 3": False}


@pytest.mark.asyncio
async def test_checklist_is_scoped_to_the_signed_in_user(
    client, db_session, created_user, second_user_headers
):
    """Ownership is per-collector: another signed-in user must see the same
    catalog with everything unowned, never the first user's holdings."""
    cset, cards = await _set_with_cards(db_session, numbers=["1", "2"], total_cards=2)
    await _own(db_session, created_user, cards[0])

    body = assert_envelope_ok(
        await client.get(f"/v1/sets/{cset.id}/checklist", headers=second_user_headers)
    )
    assert body["owned"] == 0
    assert all(c["owned"] is False for c in body["cards"])


@pytest.mark.asyncio
async def test_checklist_requires_auth(client, db_session):
    """`owned` is user data, so there is no anonymous view of the checklist."""
    cset, _ = await _set_with_cards(db_session, numbers=["1"], total_cards=1)
    resp = await client.get(f"/v1/sets/{cset.id}/checklist")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_checklist_404_for_unknown_set(client, auth_headers):
    resp = await client.get(f"/v1/sets/{uuid.uuid4()}/checklist", headers=auth_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_checklist_rejects_a_non_uuid_set_id(client, auth_headers):
    """`set_id` is a UUID path param — a garbage id is a client error (422),
    not a 404, so the client can tell "bad request" from "no such set"."""
    resp = await client.get("/v1/sets/not-a-uuid/checklist", headers=auth_headers)
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_checklist_total_falls_back_to_the_declared_set_size(
    client, auth_headers, db_session
):
    """With nothing indexed for the set, `total` comes from the set's own
    declared `total_cards` so the UI can still show "0 of 102" honestly."""
    cset = CardSet(
        tcg=TcgEnum.pokemon,
        name=f"Empty Set {uuid.uuid4().hex[:6]}",
        code=None,
        total_cards=102,
    )
    db_session.add(cset)
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get(f"/v1/sets/{cset.id}/checklist", headers=auth_headers)
    )
    assert body["cards"] == []
    assert body["owned"] == 0
    assert body["total"] == 102
