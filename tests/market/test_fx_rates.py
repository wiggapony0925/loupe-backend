"""FX rates service + endpoint — the single conversion table for all clients.

No test here touches the network: upstream fetches are monkeypatched, and
the cache layer is the test database's kv_cache table.
"""

from __future__ import annotations

import pytest

from app.services.market import fx_service


@pytest.fixture(autouse=True)
async def _clear_fx_cache(db_session):
    """Each test starts with a cold FX cache."""
    from sqlalchemy import delete

    from app.models.kv_cache import KvCacheEntry

    await db_session.execute(delete(KvCacheEntry))
    await db_session.commit()
    yield


@pytest.mark.anyio
async def test_live_fetch_merges_fiat_and_crypto(monkeypatch):
    async def fake_live():
        return fx_service._complete({"EUR": 0.5, "JPY": 100.0, "BTC": 0.00002})

    monkeypatch.setattr(fx_service, "_fetch_live", fake_live)
    doc = await fx_service.get_rates(force_refresh=True)

    assert doc["base"] == "USD"
    assert doc["source"] == "live"
    assert doc["rates"]["USD"] == 1.0
    assert doc["rates"]["EUR"] == 0.5
    assert doc["rates"]["JPY"] == 100.0
    assert doc["rates"]["BTC"] == 0.00002
    # Codes the fake upstream omitted are filled from the static snapshot,
    # so clients always get a complete table.
    for code in [*fx_service.FIAT_CODES, *fx_service.CRYPTO_IDS.values()]:
        assert code in doc["rates"], code


@pytest.mark.anyio
async def test_second_call_serves_from_cache(monkeypatch):
    calls = {"n": 0}

    async def fake_live():
        calls["n"] += 1
        return fx_service._complete({"EUR": 0.77})

    monkeypatch.setattr(fx_service, "_fetch_live", fake_live)
    first = await fx_service.get_rates(force_refresh=True)
    second = await fx_service.get_rates()

    assert calls["n"] == 1, "second call must not re-fetch upstream"
    assert first["rates"]["EUR"] == second["rates"]["EUR"] == 0.77
    assert second["source"] == "cached"


@pytest.mark.anyio
async def test_upstream_failure_degrades_to_static(monkeypatch):
    async def fake_live():
        raise RuntimeError("provider down")

    monkeypatch.setattr(fx_service, "_fetch_live", fake_live)
    doc = await fx_service.get_rates(force_refresh=True)

    assert doc["source"] == "static"
    assert doc["rates"]["USD"] == 1.0
    assert doc["rates"]["JPY"] == fx_service.STATIC_RATES["JPY"]
    # Static table is complete too.
    for code in [*fx_service.FIAT_CODES, *fx_service.CRYPTO_IDS.values()]:
        assert code in doc["rates"], code


@pytest.mark.anyio
async def test_endpoint_returns_complete_table(client, monkeypatch):
    async def fake_live():
        return fx_service._complete({"EUR": 0.9, "GBP": 0.8})

    monkeypatch.setattr(fx_service, "_fetch_live", fake_live)
    res = await client.get("/v1/market/fx/rates")

    assert res.status_code == 200
    body = res.json()
    data = body.get("data") or body  # envelope-agnostic
    assert data["base"] == "USD"
    assert data["rates"]["USD"] == 1.0
    assert data["rates"]["EUR"] in (0.9, fx_service.STATIC_RATES["EUR"])
    assert "JPY" in data["rates"]
