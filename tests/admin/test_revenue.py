"""Router tests for `/v1/admin/revenue` — the subscription money page.

There is no billing table: every figure here is *derived* from Pro state on the
``users`` row. That makes the bucketing rules the whole product — get one
predicate wrong and the reported MRR is wrong in a way nobody notices until it
is quoted somewhere. These pin the rules through the HTTP surface the portal
actually calls.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.user import User
from tests.conftest import assert_envelope_ok
from tests.factories import make_user


@pytest_asyncio.fixture
async def admin_user(db_session):
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


@contextlib.contextmanager
def _prices(monthly: float, yearly: float):
    """Pin the plan prices so the money assertions are arithmetic, not config."""
    settings = get_settings()
    prev = (settings.pro_price_monthly_usd, settings.pro_price_yearly_usd)
    settings.pro_price_monthly_usd = monthly  # type: ignore[misc]
    settings.pro_price_yearly_usd = yearly  # type: ignore[misc]
    try:
        yield
    finally:
        (
            settings.pro_price_monthly_usd,  # type: ignore[misc]
            settings.pro_price_yearly_usd,  # type: ignore[misc]
        ) = prev


async def _user(db, **kw) -> User:
    user = User(
        email=f"u+{uuid.uuid4().hex[:8]}@example.com",
        apple_subject=f"apple-{uuid.uuid4().hex}",
        **kw,
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_mrr_counts_paying_subscribers_only(client, admin_headers, db_session):
    """Trials and admin comps are Pro, but they are not revenue. Modelling MRR
    off "how many people have Pro" would inflate it by every free ride we hand
    out — the one number a founder must never over-report to themselves."""
    now = datetime.now(UTC)
    await _user(db_session, plan="pro", stripe_subscription_id="sub_a", pro_since=now)
    await _user(db_session, plan="pro", stripe_subscription_id="sub_b", pro_since=now)
    await _user(
        db_session,
        plan="pro",
        stripe_subscription_id="sub_c",
        pro_trialing=True,
        pro_since=now,
    )
    await _user(db_session, plan="pro", pro_since=now)  # comped, no Stripe sub
    await db_session.commit()

    with _prices(9.99, 99.0):
        data = assert_envelope_ok(
            await client.get("/v1/admin/revenue", headers=admin_headers)
        )

    assert data["paying"] == 2
    assert data["trialing"] == 1
    assert data["comped"] == 1
    assert data["free"] == 1  # the admin account itself
    assert data["total_users"] == 5
    assert data["est_mrr_usd"] == 19.98
    assert data["est_arr_usd"] == 239.76
    assert data["currency"] == "USD"


@pytest.mark.asyncio
async def test_churn_is_measured_against_the_surviving_base(
    client, admin_headers, db_session
):
    """Churn rate is churned / (still paying + churned) — the share of the
    cohort that *could* have left and did. Dividing by total users instead
    would make the number shrink every time a free signup arrives."""
    now = datetime.now(UTC)
    await _user(db_session, plan="pro", stripe_subscription_id="sub_a", pro_since=now)
    await _user(
        db_session,
        plan="free",
        pro_since=now - timedelta(days=90),
        pro_expires_at=now - timedelta(days=3),
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    assert data["paying"] == 1
    assert data["churned_30d"] == 1
    assert data["churn_rate_30d"] == 0.5


@pytest.mark.asyncio
async def test_a_lapse_older_than_the_window_is_not_this_months_churn(
    client, admin_headers, db_session
):
    """Churn is a trailing-30-day figure. Someone who left six months ago must
    not keep re-appearing in it, or the rate never recovers."""
    now = datetime.now(UTC)
    await _user(
        db_session,
        plan="free",
        pro_since=now - timedelta(days=300),
        pro_expires_at=now - timedelta(days=180),
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    assert data["churned_30d"] == 0
    assert data["churn_rate_30d"] == 0.0


@pytest.mark.asyncio
async def test_new_pro_trend_is_a_continuous_zero_filled_run_of_months(
    client, admin_headers, db_session
):
    """The chart draws one bar per month; a month with no signups has to come
    back as zero rather than be missing, or the x-axis silently compresses."""
    await _user(
        db_session,
        plan="pro",
        stripe_subscription_id="sub_a",
        pro_since=datetime.now(UTC),
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    trend = data["pro_by_month"]
    assert len(trend) == 6
    months = [point["month"] for point in trend]
    assert months == sorted(months), "oldest → newest, the direction the chart reads"
    assert months[-1] == datetime.now(UTC).strftime("%Y-%m")
    assert trend[-1]["new_pro"] == 1
    assert all(point["new_pro"] == 0 for point in trend[:-1])


@pytest.mark.asyncio
async def test_a_stale_trial_flag_does_not_keep_a_free_user_in_the_trialing_bucket(
    client, admin_headers, db_session
):
    """A trialist is someone on a plan, not someone carrying an old flag.

    The admin "set plan" action drops a user to free without clearing
    ``pro_trialing`` (only the Stripe webhook clears it), so the bucket has to
    require a live plan as well. A demoted user counts as free — once.
    """
    await _user(
        db_session, plan="free", stripe_subscription_id="sub_x", pro_trialing=True
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    assert data["paying"] == 0
    assert data["trialing"] == 0  # nobody is on a plan
    assert data["free"] == 2  # the admin *and* the demoted user
    assert data["total_users"] == 2


@pytest.mark.asyncio
async def test_a_comped_user_with_a_stale_trial_flag_is_counted_once(
    client, admin_headers, db_session
):
    """The buckets are mutually exclusive: paying + trialing + comped + free
    must equal total_users. A comp granted to someone whose Stripe trial flag
    was never cleared is comped only — double-counting it would make the
    buckets overflow the user count and silently deflate ``free``."""
    await _user(db_session, plan="pro", pro_trialing=True)  # comped, no Stripe sub
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    assert data["comped"] == 1
    assert data["trialing"] == 0
    assert data["paying"] == 0
    assert data["free"] == 1  # the admin account itself
    assert (
        data["paying"] + data["trialing"] + data["comped"] + data["free"]
        == data["total_users"]
        == 2
    )


@pytest.mark.asyncio
async def test_an_empty_install_reports_zero_revenue_not_an_error(
    client, admin_headers
):
    """Before the first customer, every derived figure divides by zero
    somewhere. The page still has to render on day one."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/revenue", headers=admin_headers)
    )
    assert data["paying"] == data["trialing"] == data["comped"] == 0
    assert data["est_mrr_usd"] == 0.0
    assert data["churn_rate_30d"] == 0.0
    assert isinstance(data["billing_configured"], bool)
