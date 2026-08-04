"""The scheduled + hooked lifecycle email: digest, expiry, and the one-shots.

These assert the *decisions* — who gets mail and who is spared — because
that's where this class of email goes wrong: a digest to an empty vault, an
"ending soon" to someone who's auto-renewing, or a ceiling notice on every
single blocked add.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.portfolio_snapshot import PortfolioSnapshot
from app.services import email_service
from app.services.auth import device_notice_service
from app.tasks import lifecycle_email
from tests.factories import make_user


class _Recorder:
    def __init__(self, result=True):
        self.calls: list = []
        self._result = result

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._result


async def _snapshot(db, user, *, days_ago: int, value: str, count: int = 3):
    row = PortfolioSnapshot(
        user_id=user.id,
        collection_id=None,
        captured_at=datetime.now(UTC) - timedelta(days=days_ago),
        total_value_usd=Decimal(value),
        holdings_count=count,
    )
    db.add(row)
    await db.commit()
    return row


# ── Portfolio digest ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_digest_reports_the_move_across_the_window(db_session, monkeypatch):
    batch = _Recorder(result=1)
    monkeypatch.setattr(email_service, "send_portfolio_digest", batch)

    user = await make_user(db_session)
    await _snapshot(db_session, user, days_ago=6, value="1000.00")
    await _snapshot(db_session, user, days_ago=1, value="1250.00")

    result = await lifecycle_email.send_portfolio_digests()
    assert result["eligible"] == 1
    (recipients,), kwargs = batch.calls[0]
    r = next(x for x in recipients if x.user.id == user.id)
    assert r.total_value_usd == Decimal("1250.00")
    assert r.delta_usd == Decimal("250.00")
    assert r.delta_pct == pytest.approx(25.0)
    assert kwargs["period_label"] == "This week"
    # The unsubscribe link is what makes recurring mail legal to send.
    assert r.unsub_url


@pytest.mark.asyncio
async def test_digest_skips_vaults_with_nothing_to_report(db_session, monkeypatch):
    """One snapshot has no move; zero snapshots has no vault."""
    batch = _Recorder(result=0)
    monkeypatch.setattr(email_service, "send_portfolio_digest", batch)

    lonely = await make_user(db_session)
    await _snapshot(db_session, lonely, days_ago=2, value="500.00")
    await make_user(db_session)  # no snapshots at all

    result = await lifecycle_email.send_portfolio_digests()
    assert result["eligible"] == 0
    assert batch.calls == []


@pytest.mark.asyncio
async def test_digest_ignores_collection_scoped_snapshots(db_session, monkeypatch):
    """Scoped rows describe one collection — never the portfolio."""
    batch = _Recorder(result=0)
    monkeypatch.setattr(email_service, "send_portfolio_digest", batch)

    user = await make_user(db_session)
    for days, value in ((6, "10.00"), (1, "20.00")):
        row = PortfolioSnapshot(
            user_id=user.id,
            collection_id=uuid.uuid4(),
            captured_at=datetime.now(UTC) - timedelta(days=days),
            total_value_usd=Decimal(value),
            holdings_count=1,
        )
        db_session.add(row)
    await db_session.commit()

    result = await lifecycle_email.send_portfolio_digests()
    assert result["eligible"] == 0


@pytest.mark.asyncio
async def test_digest_honors_the_unsubscribe(db_session, monkeypatch):
    from sqlalchemy import select

    from app.models.user import UserSettings

    batch = _Recorder(result=0)
    monkeypatch.setattr(email_service, "send_portfolio_digest", batch)

    user = await make_user(db_session)
    await _snapshot(db_session, user, days_ago=6, value="100.00")
    await _snapshot(db_session, user, days_ago=1, value="200.00")
    row = (
        await db_session.execute(
            select(UserSettings).where(UserSettings.user_id == user.id)
        )
    ).scalar_one()
    row.email_announcements_enabled = False
    await db_session.commit()

    result = await lifecycle_email.send_portfolio_digests()
    assert result["eligible"] == 0


# ── Pro expiry notices ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expiry_notice_only_for_subscriptions_actually_ending(
    db_session, monkeypatch
):
    """The dangerous false positive: auto-renewing users must stay silent."""
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_pro_expiring", notice)

    renewing = await make_user(db_session)
    renewing.plan = "pro"
    renewing.pro_expires_at = datetime.now(UTC) + timedelta(days=7, hours=2)
    ending = await make_user(db_session)
    ending.plan = "pro"
    ending.pro_expires_at = datetime.now(UTC) + timedelta(days=7, hours=2)
    await db_session.commit()

    async def only_the_second(user):
        return user.id == ending.id

    monkeypatch.setattr(lifecycle_email, "_is_ending", only_the_second)

    result = await lifecycle_email.send_pro_expiry_notices()
    assert result["sent"] == 1
    assert notice.calls[0][0][0].id == ending.id
    assert notice.calls[0][1]["days_left"] == 7


@pytest.mark.asyncio
async def test_expiry_notice_fires_only_on_milestone_days(db_session, monkeypatch):
    """A 7-day window must not become seven emails."""
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_pro_expiring", notice)

    async def always(user):
        return True

    monkeypatch.setattr(lifecycle_email, "_is_ending", always)

    user = await make_user(db_session)
    user.plan = "pro"
    # 4 days out — between the 7- and 1-day milestones.
    user.pro_expires_at = datetime.now(UTC) + timedelta(days=4, hours=2)
    await db_session.commit()

    result = await lifecycle_email.send_pro_expiry_notices()
    assert result["sent"] == 0
    assert notice.calls == []


@pytest.mark.asyncio
async def test_expiry_notice_covers_the_whole_milestone_day(db_session, monkeypatch):
    """Regression: `days_left` truncates, so anything from 7d00h to 7d23h reads
    as 7. A candidate horizon of exactly 7 days excluded all of it except the
    instant boundary — and the next day's run sees 6, which is not a
    milestone, so the notice was silently unreachable."""
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_pro_expiring", notice)

    async def always(user):
        return True

    monkeypatch.setattr(lifecycle_email, "_is_ending", always)

    user = await make_user(db_session)
    user.plan = "pro"
    user.pro_expires_at = datetime.now(UTC) + timedelta(days=7, hours=23)
    await db_session.commit()

    result = await lifecycle_email.send_pro_expiry_notices()
    assert result["sent"] == 1
    assert notice.calls[0][1]["days_left"] == 7


@pytest.mark.asyncio
async def test_expiry_notice_is_keyed_per_milestone(db_session, monkeypatch):
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_pro_expiring", notice)

    async def always(user):
        return True

    monkeypatch.setattr(lifecycle_email, "_is_ending", always)

    user = await make_user(db_session)
    user.plan = "pro"
    user.pro_expires_at = datetime.now(UTC) + timedelta(days=1, hours=2)
    await db_session.commit()

    await lifecycle_email.send_pro_expiry_notices()
    key = notice.calls[0][1]["idempotency_key"]
    assert str(user.id) in key and key.endswith("-1")


# ── Device notices ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ua,expected",
    [
        (
            (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) AppleWebKit/605.1.15 "
                "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
            ),
            "iPhone · Safari",
        ),
        (
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
            "Windows · Chrome",
        ),
        # Edge and Chrome both claim to be Safari; order must win correctly.
        (
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Edg/130.0"
            ),
            "Mac · Edge",
        ),
        ("LoupeApp/1.4.2 (iOS 18.0)", "LoupeApp/1.4.2"),
        (None, "Unrecognized device"),
    ],
)
def test_describe_device(ua, expected):
    assert device_notice_service.describe_device(ua) == expected


def test_client_ip_prefers_the_original_client():
    assert (
        device_notice_service.client_ip(
            {"x-forwarded-for": "203.0.113.9, 10.0.0.1, 10.0.0.2"}, "10.0.0.2"
        )
        == "203.0.113.9"
    )
    assert device_notice_service.client_ip({}, "192.0.2.5") == "192.0.2.5"
    assert device_notice_service.client_ip(None, None) is None


@pytest.mark.asyncio
async def test_new_device_notifies_once_then_stays_quiet(db_session, monkeypatch):
    seen: dict[str, str] = {}

    async def fake_get(key):
        return seen.get(key)

    async def fake_set(key, value, ttl):
        seen[key] = value

    monkeypatch.setattr(device_notice_service, "kv_get", fake_get)
    monkeypatch.setattr(device_notice_service, "kv_set", fake_set)
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_new_sign_in", notice)

    user = await make_user(db_session)
    ua = "Mozilla/5.0 (Macintosh) Chrome/130.0 Safari/537.36"

    assert await device_notice_service.notify_if_new_device(
        user, user_agent=ua, ip="203.0.113.9", account_age_seconds=99_999
    )
    assert len(notice.calls) == 1
    # Same device again — recognized, silent.
    await device_notice_service.notify_if_new_device(
        user, user_agent=ua, ip="203.0.113.9", account_age_seconds=99_999
    )
    assert len(notice.calls) == 1
    # A different device is a new alert.
    await device_notice_service.notify_if_new_device(
        user,
        user_agent="Mozilla/5.0 (Windows NT 10.0) Firefox/130.0",
        ip="203.0.113.9",
        account_age_seconds=99_999,
    )
    assert len(notice.calls) == 2


@pytest.mark.asyncio
async def test_signup_does_not_alert_but_records_the_device(db_session, monkeypatch):
    """ "New sign-in" four seconds after choosing a password is just noise."""
    seen: dict[str, str] = {}

    async def fake_get(key):
        return seen.get(key)

    async def fake_set(key, value, ttl):
        seen[key] = value

    monkeypatch.setattr(device_notice_service, "kv_get", fake_get)
    monkeypatch.setattr(device_notice_service, "kv_set", fake_set)
    notice = _Recorder()
    monkeypatch.setattr(email_service, "send_new_sign_in", notice)

    user = await make_user(db_session)
    ua = "Mozilla/5.0 (Macintosh) Chrome/130.0 Safari/537.36"
    sent = await device_notice_service.notify_if_new_device(
        user, user_agent=ua, ip="203.0.113.9", account_age_seconds=5
    )
    assert sent is False
    assert notice.calls == []
    # But the device is remembered, so the *next* sign-in is silent too.
    assert seen, "signup device must still be recorded"
