"""Wave 2 tests — revenue analytics + user account actions."""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.models.user import User
from app.services.admin import revenue_service


@contextlib.contextmanager
def _as_admin(email: str):
    settings = get_settings()
    prev = settings.admin_emails
    settings.admin_emails = email  # type: ignore[misc]
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
    await db.flush()
    return user


# ── revenue_service ──
@pytest.mark.asyncio
async def test_revenue_summary_buckets(db_session):
    now = datetime.now(UTC)
    await _mk(db_session, plan="pro", stripe_subscription_id="sub_a", pro_since=now)
    await _mk(
        db_session,
        plan="pro",
        stripe_subscription_id="sub_b",
        pro_trialing=True,
        pro_since=now,
    )
    await _mk(db_session, plan="pro", pro_since=now)  # comped (no stripe sub)
    await _mk(db_session, plan="free")
    await db_session.commit()

    s = await revenue_service.summary(db_session)
    assert s.paying == 1
    assert s.trialing == 1
    assert s.comped == 1
    assert s.free == 1
    assert s.total_users == 4
    assert s.est_mrr_usd == round(get_settings().pro_price_monthly_usd, 2)
    assert s.est_arr_usd == round(s.est_mrr_usd * 12, 2)
    assert s.new_pro_30d == 3
    assert s.churned_30d == 0
    assert len(s.pro_by_month) == 6  # a continuous run of months


# ── revoke sessions ──
@pytest.mark.asyncio
async def test_revoke_sessions_requires_admin(client, created_user):
    resp = await client.post(f"/v1/admin/users/{created_user.id}/revoke-sessions")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_revoke_sessions_bumps_token_version(
    client, created_user, auth_headers, db_session
):
    uid = created_user.id
    assert created_user.token_version == 0
    with _as_admin(created_user.email):
        resp = await client.post(
            f"/v1/admin/users/{uid}/revoke-sessions", headers=auth_headers
        )
    assert resp.status_code == 200
    db_session.expire_all()
    refreshed = await db_session.get(User, uid)
    assert refreshed is not None
    assert refreshed.token_version == 1


# ── cancel subscription (super-admin gated) ──
@pytest.mark.asyncio
async def test_cancel_subscription_needs_super_admin(
    client, created_user, auth_headers, db_session
):
    """A DB-granted admin who is NOT in ADMIN_EMAILS is rejected (403)."""
    created_user.is_admin = True  # DB admin grant, but not a super-admin
    await db_session.commit()
    # admin_emails stays empty → require_admin passes, require_super_admin fails.
    resp = await client.post(
        f"/v1/admin/users/{created_user.id}/subscription/cancel",
        headers=auth_headers,
        json={},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cancel_subscription_no_sub_is_400(client, created_user, auth_headers):
    """Super-admin, but the target has no Stripe subscription → 400."""
    with _as_admin(created_user.email):
        resp = await client.post(
            f"/v1/admin/users/{created_user.id}/subscription/cancel",
            headers=auth_headers,
            json={},
        )
    assert resp.status_code == 400
