"""PSA provider — population parsing + env-gating."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.config import reload_settings
from app.integrations.base import close_http_client
from app.integrations.psa import PsaProvider


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_http_client()


@pytest.mark.asyncio
async def test_psa_no_token(monkeypatch):
    monkeypatch.delenv("PSA_API_TOKEN", raising=False)
    reload_settings()
    p = PsaProvider()
    assert p.is_configured() is False
    assert await p.get_population("12345") is None


@pytest.mark.asyncio
async def test_psa_population_parsed(monkeypatch):
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    reload_settings()
    p = PsaProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.psacard.com/publicapi/pop").mock(
            return_value=httpx.Response(
                200,
                json={"PSASpecPopulation": {"pop10": 5, "pop9": 12, "pop8": 0}},
            )
        )
        out = await p.get_population("12345")
    assert out is not None
    assert {(r.grade, r.population) for r in out} == {("10", 5), ("9", 12)}


@pytest.mark.asyncio
async def test_psa_failure_returns_none(monkeypatch):
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    reload_settings()
    p = PsaProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.psacard.com/publicapi/pop").mock(
            return_value=httpx.Response(404)
        )
        out = await p.get_population("missing")
    assert out is None
