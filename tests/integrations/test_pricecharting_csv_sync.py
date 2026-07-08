"""Legendary bulk CSV mirror — sync, status, and conservative mirror-first
lookup. Runs offline via the ``rows=`` path (no HTTP)."""

from __future__ import annotations

import pytest

from app.integrations.pricecharting import csv_sync

# CSV rows arrive as strings; pennies with no dot, dollars with a dot.
ROWS = [
    {
        "id": "6910",
        "product-name": "Charizard #4",
        "console-name": "Pokemon Base Set",
        "loose-price": "30000",  # $300
        "manual-only-price": "250000",  # PSA 10 $2500
        "sales-volume": "1234",
    },
    {
        "id": "58",
        "product-name": "Pikachu #58",
        "console-name": "Pokemon Base Set",
        "loose-price": "500",  # $5
    },
    {"id": "z", "product-name": "", "loose-price": "100"},  # no name → dropped
]


@pytest.mark.asyncio
async def test_sync_then_mirror_first_lookup(db_engine):
    csv_sync.reset_ready_cache()
    res = await csv_sync.sync_price_guide(rows=ROWS)
    assert res == {"ok": True, "rows": 2}  # the nameless row is dropped

    status = await csv_sync.get_status()
    assert status["ready"] is True
    assert status["rows"] == 2
    assert status["synced_at"] is not None

    mp = await csv_sync.lookup_market_price("Charizard #4")
    assert mp is not None
    assert mp.source == "pricecharting"
    assert mp.low == 300.0  # raw
    assert mp.extras["grade_ladder"] == {"UNGRADED": 300.0, "PSA 10": 2500.0}
    assert mp.extras["sales_volume"] == 1234
    assert mp.extras["source_detail"] == "mirror"


@pytest.mark.asyncio
async def test_lookup_is_exact_match_only(db_engine):
    """A non-exact name misses so we never serve the wrong printing — the caller
    falls back to the live API instead."""
    csv_sync.reset_ready_cache()
    await csv_sync.sync_price_guide(rows=ROWS)
    assert await csv_sync.lookup_market_price("Blastoise Base Set") is None


@pytest.mark.asyncio
async def test_lookup_none_when_mirror_empty(db_engine):
    csv_sync.reset_ready_cache()
    await csv_sync.sync_price_guide(rows=[])  # empties the table
    assert (await csv_sync.get_status())["ready"] is False
    assert await csv_sync.lookup_market_price("Charizard #4") is None


@pytest.mark.asyncio
async def test_sync_no_url_is_graceful_noop(monkeypatch):
    class _S:
        pricecharting_csv_url = None

    monkeypatch.setattr(csv_sync, "get_settings", lambda: _S())
    res = await csv_sync.sync_price_guide()
    assert res["ok"] is False
    assert res["reason"] == "no_csv_url"
