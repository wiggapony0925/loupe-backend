"""Router tests for the admin analytics dashboards.

Everything under ``/v1/admin`` that only *reads*: the pulse feed, engagement,
cohort retention, portal metrics, the grade-review queue, the card lineage
tree, and the "Ask your data" configuration probe.

Two rules dominate here and are worth pinning at the HTTP layer even though
the services behind them have their own tests:

1. **Nothing leaks to a non-admin.** The gate lives once on the ``/v1/admin``
   router, so a single mistake there would open every dashboard at once.
2. **An empty database is a valid answer, not a 500.** These pages are the
   first thing loaded on a fresh deploy, where every table is empty — the
   aggregation code (divisions by user counts, zero-filled trends, ``max()``
   over nothing) has to survive that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.blog import BlogPost
from app.models.career import JobApplication, JobPosting
from app.models.enums import (
    BlogStatusEnum,
    GradeHouseEnum,
    JobStatusEnum,
    ScanSourceEnum,
    ScanStatusEnum,
    WaitlistStatusEnum,
)
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.models.waitlist import WaitlistEntry
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_card, make_user

#: Every read-only dashboard that needs no query parameters and touches no
#: network. Parametrising keeps a newly added page from quietly skipping the
#: "empty database" check.
DASHBOARDS = [
    "/v1/admin/pulse",
    "/v1/admin/engagement",
    "/v1/admin/retention",
    "/v1/admin/metrics",
    "/v1/admin/revenue",
    "/v1/admin/grades",
    "/v1/admin/card-tree",
    "/v1/admin/insights/status",
]

#: The gate is enforced before any handler runs, so `pricecharting` — which
#: would otherwise probe a live account — is safe to include here.
GATED_PATHS = [*DASHBOARDS, "/v1/admin/pricecharting"]


@pytest_asyncio.fixture
async def admin_user(db_session):
    """A Loupe staff account via the DB-backed grant (not the env allowlist)."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.parametrize("path", GATED_PATHS)
@pytest.mark.asyncio
async def test_dashboards_reject_anonymous_callers(client, path):
    """No admin analytics without a token — these expose the whole business."""
    assert_envelope_error(await client.get(path), expected_status=401)


@pytest.mark.parametrize("path", GATED_PATHS)
@pytest.mark.asyncio
async def test_dashboards_reject_ordinary_users(client, auth_headers, path):
    """A signed-in collector is not staff. Revenue, retention and the user
    pulse are all operator-only, so authentication alone is never enough."""
    assert_envelope_error(
        await client.get(path, headers=auth_headers), expected_status=403
    )


@pytest.mark.asyncio
async def test_db_admin_grant_is_enough_without_the_env_allowlist(
    client, admin_headers
):
    """``is_admin`` on the row admits you: the ``ADMIN_EMAILS`` bootstrap is a
    way *in*, not the only way. Colleagues promoted from the portal must not
    need a redeploy to read the dashboards."""
    assert_envelope_ok(await client.get("/v1/admin/metrics", headers=admin_headers))


# ── Empty database ────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", DASHBOARDS)
@pytest.mark.asyncio
async def test_dashboards_answer_on_an_empty_database(client, admin_headers, path):
    """Day one of a deployment has no scans, no grades and no revenue. Every
    page still has to render — a divide-by-zero here breaks the whole portal."""
    data = assert_envelope_ok(await client.get(path, headers=admin_headers))
    assert data is not None


# ── Pulse ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pulse_surfaces_a_signup_as_an_event(client, admin_headers, admin_user):
    """The feed's whole purpose is "what just happened" — the admin's own
    signup is the one event that always exists."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/pulse", headers=admin_headers)
    )
    signups = [e for e in data["events"] if e["type"] == "signup"]
    assert len(signups) == 1
    assert signups[0]["id"] == f"signup:{admin_user.id}"
    assert signups[0]["title"] == "New sign-up"


@pytest.mark.asyncio
async def test_pulse_limit_caps_the_feed(client, admin_headers, db_session):
    """`limit` is the client's page size, not a hint — an operator loading the
    dashboard on a busy day must not receive the entire history."""
    for _ in range(3):
        await make_user(db_session)
    data = assert_envelope_ok(
        await client.get("/v1/admin/pulse", headers=admin_headers, params={"limit": 2})
    )
    assert len(data["events"]) == 2


@pytest.mark.parametrize("limit", [0, 101])
@pytest.mark.asyncio
async def test_pulse_rejects_out_of_range_limits(client, admin_headers, limit):
    """The bound is declared on the query parameter, so an unbounded feed can
    never be requested by hand."""
    resp = await client.get(
        "/v1/admin/pulse", headers=admin_headers, params={"limit": limit}
    )
    assert_envelope_error(resp, expected_status=422)


# ── Engagement ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engagement_counts_a_card_add_as_activation(
    client, admin_headers, db_session
):
    """ "Activated" means the user actually put a card in their vault. With two
    accounts and one collector, the rate is exactly one half — the number the
    growth page leads with, so it must not drift with the admin's own row."""
    collector = await make_user(db_session)
    card = await make_card(db_session)
    db_session.add(
        GradedCard(
            user_id=collector.id,
            card_id=card.id,
            grade=Decimal("9.0"),
            house=GradeHouseEnum.psa,
            graded_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/engagement", headers=admin_headers)
    )
    assert data["total_users"] == 2  # the admin + the collector
    assert data["activated_users"] == 1
    assert data["activation_rate"] == 0.5
    # A fresh add is activity in every window.
    assert data["active_7d"] == data["active_30d"] == data["active_90d"] == 1
    # The funnel can only narrow.
    assert [step["count"] for step in data["funnel"]] == [2, 1, 0]
    assert len(data["new_users_by_week"]) == 8


@pytest.mark.asyncio
async def test_engagement_rates_are_zero_when_nobody_signed_up(client, admin_headers):
    """The rate is a share of total users, and the portal's own admin is the
    only account on a brand-new install — so rates stay defined (never NaN)."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/engagement", headers=admin_headers)
    )
    assert data["activated_users"] == 0
    assert data["activation_rate"] == 0.0
    assert data["pro_rate"] == 0.0


# ── Retention ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retention_returns_a_narrowing_triangle(client, admin_headers):
    """Cohorts run oldest → newest and each row can only report the weeks that
    have actually elapsed — that shape *is* the triangle the page draws."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/retention", headers=admin_headers)
    )
    assert data["weeks"] == 8
    assert [len(c["retention"]) for c in data["cohorts"]] == [8, 7, 6, 5, 4, 3, 2, 1]
    assert all(0.0 <= v <= 1.0 for c in data["cohorts"] for v in c["retention"])


@pytest.mark.asyncio
async def test_retention_puts_an_active_new_user_in_the_newest_cohort(
    client, admin_headers, db_session
):
    """A user who signed up today and scanned today is 100% retained in week
    0 of the newest cohort — the anchor value every other cell is read against."""
    collector = await make_user(db_session)
    db_session.add(
        ScanJob(
            user_id=collector.id,
            status=ScanStatusEnum.complete,
            source=ScanSourceEnum.phone,
        )
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/retention", headers=admin_headers)
    )
    newest = data["cohorts"][-1]
    assert newest["size"] == 2  # admin + collector, both signed up this week
    assert newest["retention"] == [0.5]  # only the collector was active


# ── Portal metrics ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_counts_every_portal_surface(client, admin_headers, db_session):
    """One number per portal tab. They come from five different tables, so a
    seeded row of each is the cheapest guard against a mis-joined count."""
    job = JobPosting(
        slug=f"role-{uuid.uuid4().hex[:6]}",
        title="Backend Engineer",
        team="Platform",
        location="Remote",
        summary="Build the API.",
        status=JobStatusEnum.open.value,
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add_all(
        [
            JobApplication(
                job_id=job.id,
                applicant_name="Ada",
                applicant_email="ada@example.com",
            ),
            BlogPost(
                slug=f"post-{uuid.uuid4().hex[:6]}",
                title="Hello",
                body="Body",
                status=BlogStatusEnum.published.value,
            ),
            WaitlistEntry(
                email=f"w+{uuid.uuid4().hex[:6]}@example.com",
                status=WaitlistStatusEnum.waiting.value,
            ),
        ]
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/metrics", headers=admin_headers)
    )
    assert data["users_total"] == 1
    assert data["users_new_7d"] == 1
    assert data["admins"] == 1
    assert data["banned"] == 0
    assert data["jobs_total"] == data["jobs_open"] == 1
    assert data["applications_total"] == data["applications_new_7d"] == 1
    assert data["posts_total"] == data["posts_published"] == 1
    assert data["waitlist_total"] == data["waitlist_waiting"] == 1


@pytest.mark.asyncio
async def test_metrics_excludes_deleted_users_from_the_headline_count(
    client, admin_headers, db_session
):
    """A deleted account is gone as far as the business is concerned — leaving
    it in ``users_total`` would make every growth number quietly wrong."""
    ghost = await make_user(db_session)
    ghost.deleted_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/metrics", headers=admin_headers)
    )
    assert data["users_total"] == 1


# ── Grade-review queue ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grade_queue_defaults_to_first_party_loupe_grades(
    client, admin_headers, db_session
):
    """The queue exists to QA *our* grades; a PSA slab needs no review from us,
    so the unfiltered page must not bury Loupe grades under third-party ones."""
    collector = await make_user(db_session)
    loupe_card = await make_card(db_session, name="Pikachu")
    psa_card = await make_card(db_session, name="Charizard")
    db_session.add_all(
        [
            GradedCard(
                user_id=collector.id,
                card_id=loupe_card.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.loupe,
                graded_at=datetime.now(UTC),
            ),
            GradedCard(
                user_id=collector.id,
                card_id=psa_card.id,
                grade=Decimal("10"),
                house=GradeHouseEnum.psa,
                graded_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    data = assert_envelope_ok(
        await client.get("/v1/admin/grades", headers=admin_headers)
    )
    assert data["total"] == 1
    assert data["results"][0]["card_name"] == "Pikachu"
    assert data["results"][0]["user_email"] == collector.email
    # Both houses still populate the filter dropdown.
    assert data["houses"] == ["loupe", "psa"]

    everything = assert_envelope_ok(
        await client.get(
            "/v1/admin/grades", headers=admin_headers, params={"house": "all"}
        )
    )
    assert everything["total"] == 2


@pytest.mark.asyncio
async def test_grade_queue_rejects_an_oversized_page(client, admin_headers):
    """A page size cap keeps one operator from pulling every grade ever issued
    in a single request."""
    resp = await client.get(
        "/v1/admin/grades", headers=admin_headers, params={"page_size": 101}
    )
    assert_envelope_error(resp, expected_status=422)


# ── Card lineage tree ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_card_tree_describes_the_price_fallback_chain(client, admin_headers):
    """Pure metadata — no DB, no upstream call. It documents *where each field
    comes from*, which is the answer to "why is this card priced like that?"."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/card-tree", headers=admin_headers)
    )
    assert data["card_model"]["name"] == "UnifiedCard"
    assert data["set_model"]["name"] == "UnifiedSet"
    chain = data["price_chain"]
    assert chain, "the fallback chain must never be empty"
    # Ordered top-down, each rung reporting whether it is actually configured.
    assert [rung["order"] for rung in chain] == list(range(1, len(chain) + 1))
    assert all(isinstance(rung["configured"], bool) for rung in chain)
    # The metered-provider budget is the cost-control story the page tells.
    assert data["budgets"][0]["integration"] == "apitcg"


# ── Ask-your-data status probe ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insights_status_reports_whether_a_model_key_is_present(
    client, admin_headers
):
    """The portal hides the "Ask your data" box when there's no model key, so
    this flag must track the live setting rather than a build-time constant."""
    settings = get_settings()
    previous = settings.anthropic_api_key
    try:
        settings.anthropic_api_key = ""  # type: ignore[misc]
        off = assert_envelope_ok(
            await client.get("/v1/admin/insights/status", headers=admin_headers)
        )
        assert off == {"configured": False}

        settings.anthropic_api_key = "sk-ant-test"  # type: ignore[misc]
        on = assert_envelope_ok(
            await client.get("/v1/admin/insights/status", headers=admin_headers)
        )
        assert on == {"configured": True}
    finally:
        settings.anthropic_api_key = previous  # type: ignore[misc]
