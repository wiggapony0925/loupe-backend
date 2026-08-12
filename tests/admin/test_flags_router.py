"""Router tests for admin feature-flag CRUD (`/v1/admin/flags`).

Flags are the product's kill-switch layer: clients gate whole pages and
micro-apps on the flag map, so a wrong answer here either hides a shipped
feature from everyone or reveals an unfinished one. These pin down who is
allowed to change a flag and what each verb actually does to the row —
including the by-key upsert the in-app inspect overlay drives, which has to
work without ever knowing a flag's id.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.feature_flag import FeatureFlag
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the developer portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _create(client, headers, **kw) -> dict:
    payload = {"key": "web_markets", "label": "Markets", **kw}
    return assert_envelope_ok(
        await client.post("/v1/admin/flags", json=payload, headers=headers),
        expected_status=201,
    )


# ── authorization ──


@pytest.mark.asyncio
async def test_reading_flags_requires_a_signed_in_caller(client):
    """The admin list carries `admin_*` portal flags that the public map
    deliberately withholds, so it must never answer an anonymous request."""
    assert_envelope_error(await client.get("/v1/admin/flags"), expected_status=401)


@pytest.mark.asyncio
async def test_an_ordinary_user_cannot_read_the_admin_flag_list(client, auth_headers):
    assert_envelope_error(
        await client.get("/v1/admin/flags", headers=auth_headers), expected_status=403
    )


@pytest.mark.asyncio
async def test_an_ordinary_user_cannot_change_any_flag(client, auth_headers):
    """Every mutating verb is gated, not just the list — a non-admin who
    guessed a flag id must not be able to switch a feature on for everyone."""
    flag_id = uuid.uuid4()
    responses = [
        await client.post(
            "/v1/admin/flags",
            json={"key": "web_markets", "label": "Markets"},
            headers=auth_headers,
        ),
        await client.patch(
            f"/v1/admin/flags/{flag_id}", json={"enabled": True}, headers=auth_headers
        ),
        await client.delete(f"/v1/admin/flags/{flag_id}", headers=auth_headers),
        await client.put(
            "/v1/admin/flags/key/web_markets",
            json={"enabled": True},
            headers=auth_headers,
        ),
    ]
    for resp in responses:
        assert_envelope_error(resp, expected_status=403)


# ── create ──


@pytest.mark.asyncio
async def test_a_new_flag_is_created_switched_off(client, admin_headers):
    """A flag exists before the feature does, so the safe default is off —
    creating one must never light anything up for users by accident."""
    body = await _create(client, admin_headers)
    assert body["key"] == "web_markets"
    assert body["label"] == "Markets"
    assert body["enabled"] is False
    assert body["description"] is None


@pytest.mark.asyncio
async def test_a_flag_key_is_normalised_to_lowercase(client, admin_headers):
    """Clients look flags up by exact string, so `Web_Markets` and
    `web_markets` must not be able to become two different switches."""
    body = await _create(client, admin_headers, key="  WEB_MARKETS  ")
    assert body["key"] == "web_markets"


@pytest.mark.asyncio
async def test_a_key_that_is_not_a_safe_identifier_is_rejected(client, admin_headers):
    resp = await client.post(
        "/v1/admin/flags",
        json={"key": "web-markets!", "label": "Markets"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_reusing_an_existing_key_is_a_conflict(client, admin_headers):
    """Two rows sharing a key would make the public map's value a coin flip,
    so the second create is refused rather than silently winning."""
    await _create(client, admin_headers)
    resp = await client.post(
        "/v1/admin/flags",
        json={"key": "web_markets", "label": "Markets Again"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=409)


# ── list ──


@pytest.mark.asyncio
async def test_flags_are_listed_in_key_order(client, admin_headers):
    """The portal renders the list as-is; key order keeps a flag in the same
    place between visits instead of shuffling with insertion order."""
    await _create(client, admin_headers, key="zulu_flag", label="Zulu")
    await _create(client, admin_headers, key="alpha_flag", label="Alpha")

    rows = assert_envelope_ok(
        await client.get("/v1/admin/flags", headers=admin_headers)
    )
    assert [r["key"] for r in rows] == ["alpha_flag", "zulu_flag"]


@pytest.mark.asyncio
async def test_the_admin_list_includes_portal_flags_the_public_map_hides(
    client, admin_headers
):
    """`admin_*` flags are withheld from `/v1/flags` so the portal's surface
    stays invisible; this list is the one place an admin can still see them."""
    await _create(client, admin_headers, key="admin_health", label="Health page")

    rows = assert_envelope_ok(
        await client.get("/v1/admin/flags", headers=admin_headers)
    )
    assert "admin_health" in {r["key"] for r in rows}


# ── update ──


@pytest.mark.asyncio
async def test_toggling_a_flag_leaves_its_other_fields_alone(client, admin_headers):
    """The portal's switch sends only `enabled`; a partial update must not
    blank out the label and description an admin wrote earlier."""
    created = await _create(
        client, admin_headers, description="Markets tab on web and mobile."
    )

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/flags/{created['id']}",
            json={"enabled": True},
            headers=admin_headers,
        )
    )
    assert updated["enabled"] is True
    assert updated["label"] == "Markets"
    assert updated["description"] == "Markets tab on web and mobile."
    assert updated["id"] == created["id"]


@pytest.mark.asyncio
async def test_updating_a_flag_that_does_not_exist_is_a_404(client, admin_headers):
    resp = await client.patch(
        f"/v1/admin/flags/{uuid.uuid4()}", json={"enabled": True}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_flag_id_that_is_not_a_uuid_is_rejected(client, admin_headers):
    resp = await client.patch(
        "/v1/admin/flags/not-a-uuid", json={"enabled": True}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


# ── delete ──


@pytest.mark.asyncio
async def test_deleting_a_flag_removes_it_from_the_list(client, admin_headers):
    created = await _create(client, admin_headers)

    resp = await client.delete(
        f"/v1/admin/flags/{created['id']}", headers=admin_headers
    )
    assert resp.status_code == 204

    rows = assert_envelope_ok(
        await client.get("/v1/admin/flags", headers=admin_headers)
    )
    assert rows == []


@pytest.mark.asyncio
async def test_deleting_a_flag_that_does_not_exist_is_a_404(client, admin_headers):
    """Delete resolves the row before removing it, so a stale portal tab that
    re-sends a delete gets a clear 404 rather than a silent success."""
    resp = await client.delete(f"/v1/admin/flags/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


# ── upsert by key ──


@pytest.mark.asyncio
async def test_upserting_an_unknown_key_creates_the_flag(client, admin_headers):
    """The in-app inspect overlay toggles a component's flag knowing only its
    key, so the first toggle has to create the row rather than 404."""
    body = assert_envelope_ok(
        await client.put(
            "/v1/admin/flags/key/web_scanner",
            json={"enabled": True},
            headers=admin_headers,
        )
    )
    assert body["key"] == "web_scanner"
    assert body["enabled"] is True
    # With no label supplied the key stands in, so the row is never unnamed.
    assert body["label"] == "web_scanner"


@pytest.mark.asyncio
async def test_upserting_a_known_key_toggles_the_same_row(client, admin_headers):
    """Upsert must find the existing flag by key — creating a second row would
    break the unique key and orphan the id the portal is holding."""
    created = await _create(client, admin_headers, enabled=False)

    body = assert_envelope_ok(
        await client.put(
            "/v1/admin/flags/key/web_markets",
            json={"enabled": True},
            headers=admin_headers,
        )
    )
    assert body["id"] == created["id"]
    assert body["enabled"] is True
    # The label the admin already chose survives an unlabelled toggle.
    assert body["label"] == "Markets"


@pytest.mark.asyncio
async def test_upserting_a_key_that_is_not_a_safe_identifier_is_rejected(
    client, admin_headers
):
    """The key arrives in the path, where Pydantic's field validator can't see
    it, so the service re-checks the shape before creating anything."""
    resp = await client.put(
        "/v1/admin/flags/key/Web-Markets", json={"enabled": True}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)

    # And nothing was written on the way to the rejection.
    listed = assert_envelope_ok(
        await client.get("/v1/admin/flags", headers=admin_headers)
    )
    assert listed == []


# ── audit ──


@pytest.mark.asyncio
async def test_every_flag_change_is_attributed_to_the_admin_who_made_it(
    client, admin_headers, admin_user, db_session
):
    """Flags change what users can see with no deploy and no code review, so
    the audit trail is the only record of who flipped what."""
    created = await _create(client, admin_headers)
    await client.patch(
        f"/v1/admin/flags/{created['id']}",
        json={"enabled": True},
        headers=admin_headers,
    )
    await client.delete(f"/v1/admin/flags/{created['id']}", headers=admin_headers)

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_table == "feature_flags")
            )
        )
        .scalars()
        .all()
    )
    assert {r.action for r in rows} == {"flag.create", "flag.update", "flag.delete"}
    assert {r.user_id for r in rows} == {admin_user.id}
    assert all(r.target_id == str(created["id"]) for r in rows)


@pytest.mark.asyncio
async def test_a_rejected_create_leaves_no_row_behind(
    client, admin_headers, db_session
):
    """The duplicate-key path rolls back before raising; a leftover pending
    row would poison the next write on the same session."""
    await _create(client, admin_headers)
    await client.post(
        "/v1/admin/flags",
        json={"key": "web_markets", "label": "Dup"},
        headers=admin_headers,
    )

    keys = [k for (k,) in (await db_session.execute(select(FeatureFlag.key))).all()]
    assert keys == ["web_markets"]
