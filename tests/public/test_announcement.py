"""`GET /v1/announcement` — the global banner every client polls.

Unauthenticated on purpose: guests, signed-in users and the mobile app must
all see the same admin-set message (an outage notice is useless if only
logged-in users get it).
"""

from __future__ import annotations

import pytest

from app.models.site_config import SiteConfig
from tests.conftest import assert_envelope_ok


async def _configure(db_session, **fields) -> SiteConfig:
    cfg = SiteConfig(**fields)
    db_session.add(cfg)
    await db_session.commit()
    await db_session.refresh(cfg)
    return cfg


@pytest.mark.asyncio
async def test_announcement_is_off_until_an_admin_turns_it_on(client):
    """A brand-new deployment has no config row at all; the endpoint must
    still answer with a disabled banner rather than 404 or 500, because every
    client polls it on launch."""
    body = assert_envelope_ok(await client.get("/v1/announcement"))
    assert body["enabled"] is False
    assert body["message"] == ""
    assert body["tone"] == "info"


@pytest.mark.asyncio
async def test_enabled_announcement_carries_message_tone_and_cta(client, db_session):
    await _configure(
        db_session,
        announcement_enabled=True,
        announcement_message="Scheduled maintenance at 02:00 UTC.",
        announcement_tone="warning",
        announcement_cta_label="Status page",
        announcement_cta_href="https://status.example.com",
    )
    body = assert_envelope_ok(await client.get("/v1/announcement"))
    assert body == {
        "enabled": True,
        "message": "Scheduled maintenance at 02:00 UTC.",
        "tone": "warning",
        "cta_label": "Status page",
        "cta_href": "https://status.example.com",
    }


@pytest.mark.asyncio
async def test_a_blank_message_reads_as_disabled_even_when_the_flag_is_on(
    client, db_session
):
    """An admin who clears the text but forgets the toggle would otherwise
    ship an empty banner to every user — so an all-whitespace message is
    treated as "off"."""
    await _configure(
        db_session,
        announcement_enabled=True,
        announcement_message="   ",
    )
    body = assert_envelope_ok(await client.get("/v1/announcement"))
    assert body["enabled"] is False
    assert body["message"] == ""


@pytest.mark.asyncio
async def test_announcement_is_readable_without_auth(client, db_session):
    """No Authorization header here — that is the point of the endpoint."""
    await _configure(
        db_session,
        announcement_enabled=True,
        announcement_message="Read me",
    )
    resp = await client.get("/v1/announcement")
    assert resp.status_code == 200
    assert assert_envelope_ok(resp)["message"] == "Read me"
