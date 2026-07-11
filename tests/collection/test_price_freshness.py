"""Daily price tick for owned cards — the chart's data supply.

Regression for the "straight line" bug: with the nightly price worker
offline, vault cards nobody opened never accrued daily `price_history`
points (prod: median owned card had exactly ONE point), so `_value_on`
fell back to a flat ratio and every range rendered flat. The freshness
sweep tops up today's point for the stalest owned cards whenever the
chart is read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.card import Card
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.tasks import price_freshness
from tests.factories import make_card


async def _mk_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"fresh-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Fresh",
    )
    db.add(user)
    await db.commit()
    return user


async def _mk_holding(db, user: User, *, stale_days: int | None) -> Card:
    """A holding whose card's newest price point is *stale_days* old
    (None = no history at all)."""
    card = await make_card(db, name=f"Fresh Card {uuid.uuid4().hex[:6]}")
    if stale_days is not None:
        stale_date = (datetime.now(UTC).date() - timedelta(days=stale_days)).isoformat()
        card.card_metadata = {
            "price_history": [{"date": stale_date, "priceUsd": 10.0}],
        }
        flag_modified(card, "card_metadata")
    db.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("100.00"),
        )
    )
    await db.commit()
    return card


@pytest.mark.anyio
async def test_sweep_writes_todays_point_for_stale_cards(db_session, monkeypatch):
    user = await _mk_user(db_session)
    card = await _mk_holding(db_session, user, stale_days=5)

    async def _fake_get_card(card_id: str, *, force_refresh: bool = False):
        assert force_refresh is True, "sweep must force the upstream re-resolve"
        return {
            "id": card_id,
            "pricing_summary": {"market": {"amount": 42.5, "currency": "USD"}},
        }

    from app.services.catalog import card_search_service

    monkeypatch.setattr(card_search_service, "get_card", _fake_get_card)

    result = await price_freshness.refresh_owned_prices(user.id)
    assert result == {"considered": 1, "refreshed": 1, "recorded": 1}

    row = (
        await db_session.execute(select(Card).where(Card.id == card.id))
    ).scalar_one()
    await db_session.refresh(row)
    history = (row.card_metadata or {}).get("price_history") or []
    today = datetime.now(UTC).date().isoformat()
    assert any(e.get("date") == today and e.get("priceUsd") == 42.5 for e in history), (
        history
    )


@pytest.mark.anyio
async def test_sweep_skips_cards_already_ticked_today(db_session, monkeypatch):
    user = await _mk_user(db_session)
    await _mk_holding(db_session, user, stale_days=0)  # today's point exists

    calls: list[str] = []

    async def _fake_get_card(card_id: str, *, force_refresh: bool = False):
        calls.append(card_id)
        return

    from app.services.catalog import card_search_service

    monkeypatch.setattr(card_search_service, "get_card", _fake_get_card)

    result = await price_freshness.refresh_owned_prices(user.id)
    assert result["considered"] == 0
    assert calls == [], "fresh cards must not trigger upstream calls"


@pytest.mark.anyio
async def test_sweep_prioritises_the_stalest_and_respects_the_limit(
    db_session, monkeypatch
):
    user = await _mk_user(db_session)
    newer = await _mk_holding(db_session, user, stale_days=2)
    oldest = await _mk_holding(db_session, user, stale_days=30)
    no_history = await _mk_holding(db_session, user, stale_days=None)

    seen: list[str] = []

    async def _fake_get_card(card_id: str, *, force_refresh: bool = False):
        seen.append(card_id)
        return {"pricing_summary": {"market": {"amount": 5.0}}}

    from app.services.catalog import card_search_service

    monkeypatch.setattr(card_search_service, "get_card", _fake_get_card)

    result = await price_freshness.refresh_owned_prices(user.id, limit=2)
    assert result["considered"] == 2
    # No-history sorts before the 30-day-old point; the 2-day-old card
    # misses the cap.
    assert str(no_history.id) in seen
    assert str(oldest.id) in seen
    assert str(newer.id) not in seen
