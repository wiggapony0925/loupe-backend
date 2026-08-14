"""The ADMIN_EMAILS bootstrap must be written down as a real grant.

WHY. ``is_super_admin`` treats the env allowlist as an escape hatch — its own
docstring says "there's always a way back in even if the DB role flags get
muddled" — but nothing ever persisted the grant. So in production
``require_admin`` let the owner through while ``users.is_admin`` read false on
all 82 rows.

Two costs. The small one is that the database is confusing to read: the only
admin shows as not-an-admin, which is exactly what got reported. The large one
is that the escape hatch was the ONLY hatch — clear or mistype ADMIN_EMAILS on
a deploy and the app has zero admins and no DB-backed way back in, which is
the precise failure the bootstrap exists to prevent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.user import User
from app.services.admin.flag_seed import grant_admin_to_allowlisted
from tests.factories import make_user


@pytest.fixture
def allowlist(monkeypatch):
    """Point ADMIN_EMAILS at a known address for the duration of a test."""

    def _set(*emails: str):
        settings = get_settings()
        monkeypatch.setattr(
            type(settings),
            "admin_email_set",
            property(lambda _self: {e.lower() for e in emails}),
        )

    return _set


@pytest.mark.asyncio
async def test_an_allowlisted_account_is_granted_admin(db_session, allowlist):
    """The regression. The bootstrap now leaves a trace in the database."""
    user = await make_user(db_session, email="owner@example.test")
    await db_session.commit()
    assert user.is_admin is False

    allowlist("owner@example.test")
    granted = await grant_admin_to_allowlisted(db_session)

    assert granted == 1
    await db_session.refresh(user)
    assert user.is_admin is True


@pytest.mark.asyncio
async def test_it_is_idempotent(db_session, allowlist):
    """Runs on every boot, so a second pass must be a no-op rather than churn."""
    await make_user(db_session, email="owner@example.test")
    await db_session.commit()
    allowlist("owner@example.test")

    assert await grant_admin_to_allowlisted(db_session) == 1
    assert await grant_admin_to_allowlisted(db_session) == 0


@pytest.mark.asyncio
async def test_it_never_demotes(db_session, allowlist):
    """Removing someone from ADMIN_EMAILS must not strip their grant.

    Taking admin away is a decision for the portal, with its own guards and
    audit trail — not a side effect of editing an environment variable.
    """
    admin = await make_user(db_session, email="former@example.test")
    admin.is_admin = True
    await db_session.commit()

    allowlist("someone.else@example.test")
    await grant_admin_to_allowlisted(db_session)

    await db_session.refresh(admin)
    assert admin.is_admin is True


@pytest.mark.asyncio
async def test_it_leaves_everyone_else_alone(db_session, allowlist):
    other = await make_user(db_session, email="normal@example.test")
    await db_session.commit()

    allowlist("owner@example.test")
    await grant_admin_to_allowlisted(db_session)

    await db_session.refresh(other)
    assert other.is_admin is False


@pytest.mark.asyncio
async def test_an_empty_allowlist_does_nothing(db_session, allowlist):
    """No allowlist must not mean "grant to everyone" or crash on boot."""
    await make_user(db_session, email="normal@example.test")
    await db_session.commit()

    allowlist()
    assert await grant_admin_to_allowlisted(db_session) == 0

    rows = (await db_session.execute(select(User).where(User.is_admin))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_matching_ignores_case(db_session, allowlist):
    """Emails are stored as typed; the allowlist is lowercased."""
    user = await make_user(db_session, email="Owner@Example.Test")
    await db_session.commit()

    allowlist("owner@example.test")
    assert await grant_admin_to_allowlisted(db_session) == 1
    await db_session.refresh(user)
    assert user.is_admin is True
