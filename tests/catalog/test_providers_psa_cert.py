"""Tests for `GET /v1/providers/psa/cert/{cert_no}` — PSA cert verification.

The free PSA tier is capped at 100 lookups/day for the whole app, so this
route is deliberately auth-gated and degrades loudly (503) when PSA isn't
configured rather than pretending the cert doesn't exist.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.psa import PsaProvider
from app.integrations.registry import ProviderRegistry, set_registry_override
from tests.conftest import assert_envelope_error, assert_envelope_ok

_CERT = "12345678"


class _FakePsa(PsaProvider):
    """A configured PSA provider that answers from memory, never the network."""

    def __init__(self, cert: dict[str, Any] | None):
        super().__init__()
        self._cert = cert
        self.seen: list[str] = []

    def is_configured(self) -> bool:
        return True

    async def verify_cert(self, cert_no: str | int) -> dict[str, Any] | None:
        self.seen.append(str(cert_no))
        return self._cert


@pytest.fixture
def psa_provider(request):
    """Install a fake PSA provider into the registry for one test."""
    provider = _FakePsa(getattr(request, "param", None))
    set_registry_override(ProviderRegistry(providers=[provider]))
    yield provider
    set_registry_override(None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "psa_provider",
    [{"CertNumber": _CERT, "Subject": "Charizard", "CardGrade": "PSA 10"}],
    indirect=True,
)
async def test_verified_cert_is_returned_under_a_cert_key(
    client, auth_headers, psa_provider
):
    """The PSA payload is nested under `cert` so the response can grow other
    top-level fields (cache age, warnings) without breaking clients."""
    body = assert_envelope_ok(
        await client.get(f"/v1/providers/psa/cert/{_CERT}", headers=auth_headers)
    )
    assert body["cert"]["Subject"] == "Charizard"
    assert body["cert"]["CardGrade"] == "PSA 10"
    assert psa_provider.seen == [_CERT]


@pytest.mark.asyncio
@pytest.mark.parametrize("psa_provider", [None], indirect=True)
async def test_unknown_cert_is_a_404(client, auth_headers, psa_provider):
    """A cert PSA can't verify is genuinely missing, not a server fault."""
    assert_envelope_error(
        await client.get(f"/v1/providers/psa/cert/{_CERT}", headers=auth_headers),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_anonymous_callers_cannot_burn_the_psa_quota(client):
    """Auth is the quota guard: 100 lookups/day across the whole app means an
    unauthenticated caller could exhaust it for every real user."""
    resp = await client.get(f"/v1/providers/psa/cert/{_CERT}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_unconfigured_psa_reports_503_rather_than_a_missing_cert(
    client, auth_headers
):
    """With no PSA provider wired up (the default test registry is empty) the
    honest answer is "this feature is off", not "no such cert" — a 404 would
    tell a collector their real slab is fake."""
    assert_envelope_error(
        await client.get(f"/v1/providers/psa/cert/{_CERT}", headers=auth_headers),
        expected_status=503,
    )
