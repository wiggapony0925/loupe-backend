"""Tests for super-admin impersonation (gating + the token resolves to target)."""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.user import User


@contextlib.contextmanager
def _admin_emails(value: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = value  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


async def _mk(db, **kw) -> User:
    user = User(
        email=f"u+{uuid.uuid4().hex[:8]}@example.com",
        apple_subject=f"apple-{uuid.uuid4().hex}",
        **kw,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _headers(user: User) -> dict[str, str]:
    token, _ = issue_token(user.id, "access", {"ver": user.token_version})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_impersonate_requires_super_admin(client, db_session):
    actor = await _mk(db_session, is_admin=True)  # DB admin, not super
    target = await _mk(db_session)
    resp = await client.post(
        f"/v1/admin/users/{target.id}/impersonate", headers=_headers(actor)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_impersonate_returns_token_for_target(client, db_session):
    actor = await _mk(db_session)
    target = await _mk(db_session)
    with _admin_emails(actor.email):
        resp = await client.post(
            f"/v1/admin/users/{target.id}/impersonate", headers=_headers(actor)
        )
        assert resp.status_code == 200
        token = resp.json()["data"]["token"]
        # The minted token resolves to the TARGET, not the acting admin.
        me = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["email"] == target.email


@pytest.mark.asyncio
async def test_cannot_impersonate_super_admin(client, db_session):
    actor = await _mk(db_session)
    target = await _mk(db_session)
    with _admin_emails(f"{actor.email},{target.email}"):  # both super
        resp = await client.post(
            f"/v1/admin/users/{target.id}/impersonate", headers=_headers(actor)
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cannot_impersonate_self(client, db_session):
    actor = await _mk(db_session)
    with _admin_emails(actor.email):
        resp = await client.post(
            f"/v1/admin/users/{actor.id}/impersonate", headers=_headers(actor)
        )
    assert resp.status_code == 400
