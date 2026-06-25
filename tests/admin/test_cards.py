"""Tests for the admin card explorer + manual price override."""

from __future__ import annotations

import contextlib
from decimal import Decimal

import pytest

from app.config import get_settings
from app.models.card import Card, CardSet
from app.models.card_external_ref import CardExternalRef
from app.models.enums import GradeHouseEnum, TcgEnum
from app.schemas.card_admin import PriceOverrideRequest
from app.services.admin import card_admin_service


@contextlib.contextmanager
def _as_admin(email: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


async def _seed_card(db, *, name="Pikachu", number="58") -> Card:
    cs = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db.add(cs)
    await db.flush()
    card = Card(set_id=cs.id, tcg=TcgEnum.pokemon, name=name, number=number)
    db.add(card)
    await db.flush()
    return card


# ── service ──
@pytest.mark.asyncio
async def test_card_search_and_detail(db_session):
    card = await _seed_card(db_session)
    db_session.add(
        CardExternalRef(
            card_id=card.id,
            source="pokemontcg",
            external_id="base1-58",
            confidence=Decimal("0.90"),
        )
    )
    await db_session.commit()

    page = await card_admin_service.search(db_session, q="pika")
    assert page.total == 1
    assert page.results[0].name == "Pikachu"
    assert page.results[0].set_name == "Base Set"

    detail = await card_admin_service.get_detail(db_session, card.id)
    assert detail.number == "58"
    assert len(detail.external_refs) == 1
    assert detail.external_refs[0].source == "pokemontcg"


@pytest.mark.asyncio
async def test_price_override_creates_manual_snapshot(db_session):
    card = await _seed_card(db_session)
    await db_session.commit()

    snap = await card_admin_service.add_price_override(
        db_session,
        card.id,
        PriceOverrideRequest(house=GradeHouseEnum.psa, grade=10, price_usd=500),
    )
    assert snap.source == "manual"
    assert snap.price_usd == 500.0
    assert snap.house == "psa"

    detail = await card_admin_service.get_detail(db_session, card.id)
    assert len(detail.prices) == 1
    assert detail.prices[0].source == "manual"


# ── endpoint gating ──
@pytest.mark.asyncio
async def test_price_override_requires_super_admin(
    client, created_user, auth_headers, db_session
):
    card = await _seed_card(db_session)
    created_user.is_admin = True  # DB admin, but not super
    await db_session.commit()
    cid = card.id

    resp = await client.post(
        f"/v1/admin/cards/{cid}/price",
        headers=auth_headers,
        json={"house": "psa", "grade": 10, "price_usd": 100},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_card_search_endpoint(client, created_user, auth_headers, db_session):
    await _seed_card(db_session)
    await db_session.commit()
    with _as_admin(created_user.email):
        resp = await client.get("/v1/admin/cards", headers=auth_headers, params={"q": "pika"})
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] >= 1
