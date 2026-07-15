"""`/v1/app/config` — the remote-config contract the mobile client boots on."""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_ok


@pytest.mark.asyncio
async def test_app_config_shape_and_discovery_rails(client):
    resp = await client.get("/v1/app/config")
    body = assert_envelope_ok(resp)
    assert body["forceUpdate"] is False
    assert isinstance(body["flags"], dict)
    assert isinstance(body["homeRails"], list)
    # The backend owns which discovery rails show and in what order; the
    # clients render this list verbatim (unknown ids skipped). "newestSets"
    # is the backend-defined "Newest sets" carousel.
    assert "newestSets" in body["discoveryRails"]
    assert body["discoveryRails"].index("newestSets") < body["discoveryRails"].index(
        "sealedProducts"
    )


@pytest.mark.asyncio
async def test_app_config_force_update_below_min(client):
    resp = await client.get("/v1/app/config", params={"clientVersion": "0.0.1"})
    body = assert_envelope_ok(resp)
    assert body["forceUpdate"] is True


@pytest.mark.asyncio
async def test_config_serves_ai_search_limits(client):
    # The limits ride the remote config so a backend constant change reaches
    # installed clients on the next refresh — no app-store release.
    from app.services import ai

    resp = await client.get("/v1/app/config")
    body = resp.json()["data"]
    assert body["aiSearch"]["queryMaxChars"] == ai.QUERY_MAX_CHARS
    assert body["aiSearch"]["messageMaxChars"] == ai.MESSAGE_MAX_CHARS
    # Availability depends on whether the environment has a model key (dev
    # .env yes, CI no) — the kill-switch behaviour itself is pinned in
    # tests/ai/test_health.py; here we only require the switch to exist.
    assert isinstance(body["aiSearch"]["enabled"], bool)
