"""PSA provider — cert verification + env-gating + Redis caching."""

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
    monkeypatch.setenv("PSA_API_TOKEN", "")
    reload_settings()
    p = PsaProvider()
    assert p.is_configured() is False
    assert await p.verify_cert("12345") is None


@pytest.mark.asyncio
async def test_psa_verify_cert_success(monkeypatch):
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    reload_settings()
    p = PsaProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(
            url__startswith="https://api.psacard.com/publicapi/cert/GetByCertNumber/"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "PSACert": {
                        "CertNumber": "12345",
                        "Subject": "Charizard",
                        "Year": "1999",
                        "Brand": "Pokemon",
                        "CardGrade": "PSA 10",
                    },
                    "IsValidRequest": True,
                    "ServerMessage": "Request successful",
                },
            )
        )
        out = await p.verify_cert("12345")
    assert out is not None
    assert out["Subject"] == "Charizard"
    assert out["CardGrade"] == "PSA 10"
    assert "ServerMessage" not in out  # envelope stripped


@pytest.mark.asyncio
async def test_psa_verify_cert_not_found(monkeypatch):
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    reload_settings()
    p = PsaProvider()
    with respx.mock(assert_all_called=False) as router:
        router.get(
            url__startswith="https://api.psacard.com/publicapi/cert/GetByCertNumber/"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"IsValidRequest": True, "ServerMessage": "No data found"},
            )
        )
        out = await p.verify_cert("99999999")
    assert out is None


@pytest.mark.asyncio
async def test_psa_verify_cert_invalid_input(monkeypatch):
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    reload_settings()
    p = PsaProvider()
    # Non-numeric input strips down to nothing → short-circuits without HTTP.
    assert await p.verify_cert("abc") is None
    assert await p.verify_cert("") is None
