"""Write-on-read card price history (`record_price_observation`).

The nightly price worker is offline in prod, so serving a card's market
snapshot is what persists today's raw price into
``card_metadata['price_history']`` — the series that bends every modeled
portfolio range. Proves: local-UUID path, upstream-id path, same-day
no-op, and rejection of junk prices.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.card_external_ref import CardExternalRef
from app.tasks.price_snapshot import record_price_observation
from tests.factories import make_card


async def _history(db, card_id) -> list[dict]:
    # The observation writes through its own session — expire this one's
    # identity map so the read reflects the committed row, not the cache.
    db.expire_all()
    card = (await db.execute(select(Card).where(Card.id == card_id))).scalar_one()
    meta = card.card_metadata or {}
    return meta.get("price_history") or []


@pytest.mark.anyio
async def test_records_today_for_local_uuid(db_session, monkeypatch):
    card = await make_card(db_session, name="Obs Card")

    wrote = await record_price_observation(str(card.id), 123.45)

    assert wrote is True
    hist = await _history(db_session, card.id)
    assert len(hist) == 1
    assert hist[0]["priceUsd"] == 123.45


@pytest.mark.anyio
async def test_same_day_updates_in_place(db_session):
    """A repeat observation the same day refreshes today's point (latest
    price wins) without growing the series; identical prices no-op."""
    card = await make_card(db_session, name="Obs Card 2")

    first = await record_price_observation(str(card.id), 50.0)
    second = await record_price_observation(str(card.id), 55.0)
    third = await record_price_observation(str(card.id), 55.0)

    assert first is True
    assert second is True, "same-day refresh keeps today's point current"
    assert third is False, "identical price no-ops"
    hist = await _history(db_session, card.id)
    assert len(hist) == 1
    assert hist[0]["priceUsd"] == 55.0


@pytest.mark.anyio
async def test_resolves_upstream_id_via_external_ref(db_session):
    card = await make_card(db_session, name="Obs Card 3")
    db_session.add(
        CardExternalRef(card_id=card.id, source="pokemontcg", external_id="obs-3")
    )
    await db_session.commit()

    wrote = await record_price_observation("pokemontcg:obs-3", 77.0)

    assert wrote is True
    hist = await _history(db_session, card.id)
    assert hist and hist[0]["priceUsd"] == 77.0


@pytest.mark.anyio
async def test_rejects_junk(db_session):
    card = await make_card(db_session, name="Obs Card 4")
    assert await record_price_observation(str(card.id), 0) is False
    assert await record_price_observation("not-a-card", 10.0) is False
    assert await _history(db_session, card.id) == []
