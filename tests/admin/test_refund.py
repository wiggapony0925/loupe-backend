"""Tests for the super-admin refund action (gating + graceful degradation)."""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.user import User


@contextlib.contextmanager
def _as_admin(email: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = prev  # type: ignore[misc]


async def _mk_user(db, **kw) -> tuple[User, dict[str, str]]:
    """Create a user in the test body + mint its bearer token (avoids a
    fixture-ordering quirk between `client` and the in-memory engine)."""
    user = User(
        email=f"u+{uuid.uuid4().hex[:8]}@example.com",
        apple_subject=f"apple-{uuid.uuid4().hex}",
        **kw,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token, _ = issue_token(user.id, "access")
    return user, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_refund_requires_super_admin(client, db_session):
    user, headers = await _mk_user(db_session, is_admin=True)  # DB admin, not super
    resp = await client.post(f"/v1/admin/users/{user.id}/refund", headers=headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_refund_no_billing_account_is_400(client, db_session):
    """Super-admin, but the target has no Stripe customer → 400."""
    user, headers = await _mk_user(db_session)
    with _as_admin(user.email):
        resp = await client.post(f"/v1/admin/users/{user.id}/refund", headers=headers)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_refund_billing_off_is_503(client, db_session):
    """Target has a Stripe customer but billing isn't configured → 503."""
    user, headers = await _mk_user(db_session, stripe_customer_id="cus_test")
    with _as_admin(user.email):
        resp = await client.post(f"/v1/admin/users/{user.id}/refund", headers=headers)
    assert resp.status_code == 503
