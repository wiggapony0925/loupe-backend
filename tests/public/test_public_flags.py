"""`GET /v1/flags` — the public feature-flag map clients gate UI on.

Unauthenticated so guests, signed-in users and the mobile app all read the
same source of truth; `admin_*` keys are withheld because leaking their names
would map out the developer portal for anyone who curls the endpoint.
"""

from __future__ import annotations

import pytest

from app.models.feature_flag import FeatureFlag
from tests.conftest import assert_envelope_ok


async def _flag(db_session, key: str, *, enabled: bool) -> FeatureFlag:
    row = FeatureFlag(key=key, label=key.replace("_", " ").title(), enabled=enabled)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_flag_map_is_empty_when_nothing_is_configured(client):
    """The client treats a missing key as "off", so an empty object is a
    valid answer — it must not 404 or return null."""
    assert assert_envelope_ok(await client.get("/v1/flags")) == {}


@pytest.mark.asyncio
async def test_flag_map_reports_each_key_with_its_enabled_state(client, db_session):
    await _flag(db_session, "web_markets", enabled=True)
    await _flag(db_session, "mobile_scanner", enabled=False)

    body = assert_envelope_ok(await client.get("/v1/flags"))
    assert body == {"web_markets": True, "mobile_scanner": False}


@pytest.mark.asyncio
async def test_admin_prefixed_flags_are_withheld_from_the_public_map(
    client, db_session
):
    """`admin_*` flags gate the developer portal. Publishing their keys would
    advertise the portal's surface to every anonymous caller, so they are
    served only by the admin-only flags endpoint."""
    await _flag(db_session, "admin_revenue_page", enabled=True)
    await _flag(db_session, "web_markets", enabled=True)

    body = assert_envelope_ok(await client.get("/v1/flags"))
    assert body == {"web_markets": True}
    assert not any(k.startswith("admin_") for k in body)


@pytest.mark.asyncio
async def test_flags_are_readable_without_auth(client, db_session):
    """No Authorization header — guests need the same gating as members."""
    await _flag(db_session, "web_markets", enabled=True)
    resp = await client.get("/v1/flags")
    assert resp.status_code == 200
    assert assert_envelope_ok(resp)["web_markets"] is True
