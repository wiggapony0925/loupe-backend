"""Tests for the nightly price-snapshot task.

The job appends today's live ``pricing_summary.market.amount`` into
each card's ``card_metadata['price_history']`` so the portfolio
service has real day-over-day data to compute deltas against.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.tasks.price_snapshot import (
    MAX_HISTORY_POINTS,
    _upsert_today,
    snapshot_prices,
)
from tests.factories import make_card


def _meta_with_market(price: float, history: list[dict] | None = None) -> dict:
    meta: dict = {"pricing_summary": {"market": {"amount": price}}}
    if history is not None:
        meta["price_history"] = history
    return meta


# ---------------------------------------------------------------------------
# _upsert_today pure-function tests (no DB)
# ---------------------------------------------------------------------------


def test_upsert_appends_today_when_history_empty():
    today = date(2026, 5, 28)
    out, changed = _upsert_today(None, today, 12.5)
    assert changed is True
    assert out == [{"date": "2026-05-28", "priceUsd": 12.5}]


def test_upsert_appends_today_when_history_lacks_it():
    today = date(2026, 5, 28)
    history = [{"date": "2026-05-27", "priceUsd": 10.0}]
    out, changed = _upsert_today(history, today, 12.5)
    assert changed is True
    assert out == [
        {"date": "2026-05-27", "priceUsd": 10.0},
        {"date": "2026-05-28", "priceUsd": 12.5},
    ]


def test_upsert_idempotent_when_today_unchanged():
    today = date(2026, 5, 28)
    history = [
        {"date": "2026-05-27", "priceUsd": 10.0},
        {"date": "2026-05-28", "priceUsd": 12.5},
    ]
    out, changed = _upsert_today(history, today, 12.5)
    assert changed is False
    assert out == history


def test_upsert_replaces_today_when_price_drifted():
    today = date(2026, 5, 28)
    history = [{"date": "2026-05-28", "priceUsd": 12.5}]
    out, changed = _upsert_today(history, today, 13.0)
    assert changed is True
    assert out == [{"date": "2026-05-28", "priceUsd": 13.0}]


def test_upsert_trims_to_max_history_points():
    today = date(2026, 5, 28)
    # Build a synthetic history one longer than the cap, all in the
    # past, so the cap kicks in after we append today.
    history = [
        {
            "date": (today - timedelta(days=MAX_HISTORY_POINTS - i)).isoformat(),
            "priceUsd": float(i),
        }
        for i in range(MAX_HISTORY_POINTS)
    ]
    out, changed = _upsert_today(history, today, 999.0)
    assert changed is True
    assert len(out) == MAX_HISTORY_POINTS
    assert out[-1] == {"date": today.isoformat(), "priceUsd": 999.0}
    # Oldest point dropped — the original entry at day -MAX is gone.
    assert out[0]["date"] != (today - timedelta(days=MAX_HISTORY_POINTS)).isoformat()


def test_upsert_normalises_full_iso_timestamps():
    today = date(2026, 5, 28)
    history = [{"date": "2026-05-27T12:34:56Z", "priceUsd": 10.0}]
    out, _ = _upsert_today(history, today, 12.5)
    assert out[0]["date"] == "2026-05-27"


def test_upsert_skips_malformed_entries():
    today = date(2026, 5, 28)
    history = [
        "not a dict",  # type: ignore[list-item]
        {"date": None, "priceUsd": 1.0},
        {"date": "2026-05-27", "priceUsd": 10.0},
    ]
    out, changed = _upsert_today(history, today, 12.5)
    assert changed is True
    assert out == [
        {"date": "2026-05-27", "priceUsd": 10.0},
        {"date": "2026-05-28", "priceUsd": 12.5},
    ]


# ---------------------------------------------------------------------------
# snapshot_prices end-to-end against the test DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_appends_today_for_cards_with_live_price(db_session):
    today = datetime.now(UTC).date()
    card_a = await make_card(db_session, name="Has Market")
    card_a.card_metadata = _meta_with_market(42.0)
    flag_modified(card_a, "card_metadata")
    # A second card with no market metadata — created so the snapshot has
    # something to skip; we never reference it again.
    await make_card(db_session, name="No Market")
    await db_session.commit()

    result = await snapshot_prices()

    assert result["scanned"] >= 2
    assert result["updated"] == 1
    assert result["skipped"] >= 1

    await db_session.refresh(card_a)
    history = card_a.card_metadata["price_history"]
    assert history[-1] == {"date": today.isoformat(), "priceUsd": 42.0}


@pytest.mark.asyncio
async def test_snapshot_is_idempotent_within_a_day(db_session):
    card = await make_card(db_session, name="Stable")
    card.card_metadata = _meta_with_market(99.0)
    flag_modified(card, "card_metadata")
    await db_session.commit()

    first = await snapshot_prices()
    assert first["updated"] == 1

    second = await snapshot_prices()
    assert second["updated"] == 0


@pytest.mark.asyncio
async def test_snapshot_replaces_today_when_market_moved(db_session):
    today = datetime.now(UTC).date()
    card = await make_card(db_session, name="Moving Card")
    card.card_metadata = _meta_with_market(
        50.0,
        history=[{"date": today.isoformat(), "priceUsd": 40.0}],
    )
    flag_modified(card, "card_metadata")
    await db_session.commit()

    result = await snapshot_prices()
    assert result["updated"] == 1

    await db_session.refresh(card)
    history = card.card_metadata["price_history"]
    today_entries = [e for e in history if e["date"] == today.isoformat()]
    assert today_entries == [{"date": today.isoformat(), "priceUsd": 50.0}]


@pytest.mark.asyncio
async def test_snapshot_preserves_prior_history(db_session):
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    card = await make_card(db_session, name="Has Yesterday")
    card.card_metadata = _meta_with_market(
        60.0,
        history=[{"date": yesterday.isoformat(), "priceUsd": 55.0}],
    )
    flag_modified(card, "card_metadata")
    await db_session.commit()

    await snapshot_prices()

    await db_session.refresh(card)
    history = card.card_metadata["price_history"]
    assert {"date": yesterday.isoformat(), "priceUsd": 55.0} in history
    assert {"date": today.isoformat(), "priceUsd": 60.0} in history
