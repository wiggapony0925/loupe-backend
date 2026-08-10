"""The inbox over HTTP — what a real client actually receives.

The service tests pin the rows; these pin the WIRE. The envelope middleware
used to mistake ``NotificationPage`` for the legacy ``{items,total,page,
page_size}`` shape (its key set is a superset), replace ``data`` with the
bare items array, and drop ``unread`` on the floor — so every client
rendered an empty inbox with no badge, and no service-level test could ever
see it happen.
"""

from __future__ import annotations

import pytest

from app.auth.jwt import issue_token
from app.services import notification_service
from tests.conftest import assert_envelope_ok


def _headers(user) -> dict[str, str]:
    token, _ = issue_token(user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_the_inbox_page_survives_the_envelope(client, created_user, db_session):
    row = await notification_service.notify(
        db_session,
        created_user.id,
        category="system",
        kind="announcement",
        title="Welcome to Loupe",
        body="Scan your first card.",
        push=False,
    )
    assert row is not None

    data = assert_envelope_ok(
        await client.get("/v1/me/notifications", headers=_headers(created_user))
    )
    # The page must arrive as the OBJECT the clients type against —
    # not flattened into a bare list.
    assert isinstance(data, dict)
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["unread"] == 1
    assert [n["title"] for n in data["items"]] == ["Welcome to Loupe"]


@pytest.mark.asyncio
async def test_an_empty_inbox_keeps_the_same_shape(client, created_user):
    data = assert_envelope_ok(
        await client.get("/v1/me/notifications", headers=_headers(created_user))
    )
    assert isinstance(data, dict)
    assert data["items"] == []
    assert data["total"] == 0
    assert data["unread"] == 0
