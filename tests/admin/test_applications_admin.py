"""Router tests for `/v1/admin/applications` — the hiring pipeline.

Applications carry someone's name, email, LinkedIn and cover letter, so the
authorization boundary here protects real people's data rather than a metric.
Beyond that, the rules worth pinning are the ones an applicant experiences:
every stage change is appended to a trail they can read, and the outbound
notification is best-effort — a mail failure must never lose the decision.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.auth.jwt import issue_token
from app.models.career import JobApplication, JobPosting
from app.models.enums import ApplicationStatusEnum, JobStatusEnum
from tests.conftest import assert_envelope_error, assert_envelope_ok
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


async def _job(db, *, title: str = "Backend Engineer") -> JobPosting:
    job = JobPosting(
        slug=f"role-{uuid.uuid4().hex[:8]}",
        title=title,
        team="Platform",
        location="Remote",
        summary="Build the API.",
        status=JobStatusEnum.open.value,
    )
    db.add(job)
    await db.flush()
    return job


async def _application(
    db,
    job: JobPosting,
    *,
    name: str = "Ada Lovelace",
    status: str = ApplicationStatusEnum.submitted.value,
) -> JobApplication:
    row = JobApplication(
        job_id=job.id,
        applicant_name=name,
        applicant_email=f"{uuid.uuid4().hex[:8]}@example.com",
        cover_letter="I would love to work on the scanner.",
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


# ── Authorization boundary ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applications_are_invisible_without_a_token(client):
    """Cover letters and contact details are the most sensitive rows in the
    portal — an unauthenticated read would be a data breach, not a bug."""
    assert_envelope_error(
        await client.get("/v1/admin/applications"), expected_status=401
    )


@pytest.mark.asyncio
async def test_applications_are_invisible_to_ordinary_users(client, auth_headers):
    """A collector account must not be able to read who applied for a job."""
    assert_envelope_error(
        await client.get("/v1/admin/applications", headers=auth_headers),
        expected_status=403,
    )


@pytest.mark.asyncio
async def test_ordinary_users_cannot_advance_an_application(
    client, auth_headers, db_session
):
    """Deciding someone's candidacy is staff-only, and the gate has to hold on
    the write path too — not just on the list the portal renders."""
    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    resp = await client.patch(
        f"/v1/admin/applications/{application.id}/status",
        headers=auth_headers,
        json={"status": "hired", "notify": False},
    )
    assert_envelope_error(resp, expected_status=403)


# ── Listing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listing_is_empty_before_anyone_applies(client, admin_headers):
    """No applicants yet is the normal state of a new posting."""
    assert (
        assert_envelope_ok(
            await client.get("/v1/admin/applications", headers=admin_headers)
        )
        == []
    )


@pytest.mark.asyncio
async def test_listing_carries_the_job_title_so_the_table_needs_no_second_fetch(
    client, admin_headers, db_session
):
    """The pipeline table shows "Ada — Backend Engineer". Joining the title
    server-side is deliberate: the alternative is one request per row."""
    job = await _job(db_session, title="Backend Engineer")
    await _application(db_session, job, name="Ada Lovelace")
    await db_session.commit()

    rows = assert_envelope_ok(
        await client.get("/v1/admin/applications", headers=admin_headers)
    )
    assert len(rows) == 1
    assert rows[0]["applicant_name"] == "Ada Lovelace"
    assert rows[0]["job_title"] == "Backend Engineer"
    assert rows[0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_listing_filters_by_role_and_by_stage(client, admin_headers, db_session):
    """Two filters, because the two questions an operator asks are "who is in
    the pipeline for this role?" and "who is waiting on me right now?"."""
    backend = await _job(db_session, title="Backend Engineer")
    design = await _job(db_session, title="Designer")
    await _application(db_session, backend, name="Ada")
    await _application(
        db_session, backend, name="Grace", status=ApplicationStatusEnum.interview.value
    )
    await _application(db_session, design, name="Rem")
    await db_session.commit()

    by_job = assert_envelope_ok(
        await client.get(
            "/v1/admin/applications",
            headers=admin_headers,
            params={"job_id": str(backend.id)},
        )
    )
    assert {row["applicant_name"] for row in by_job} == {"Ada", "Grace"}

    by_status = assert_envelope_ok(
        await client.get(
            "/v1/admin/applications",
            headers=admin_headers,
            params={"status": "interview"},
        )
    )
    assert [row["applicant_name"] for row in by_status] == ["Grace"]


# ── Detail ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_returns_the_full_event_trail(client, admin_headers, db_session):
    """The trail is the shared record between us and the applicant — the
    detail view has to show every step, not just the current stage."""
    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    await client.patch(
        f"/v1/admin/applications/{application.id}/status",
        headers=admin_headers,
        json={"status": "reviewing", "message": "Taking a look.", "notify": False},
    )
    data = assert_envelope_ok(
        await client.get(
            f"/v1/admin/applications/{application.id}", headers=admin_headers
        )
    )
    assert data["status"] == "reviewing"
    assert [event["status"] for event in data["events"]] == ["reviewing"]
    assert data["events"][0]["message"] == "Taking a look."


@pytest.mark.asyncio
async def test_an_unknown_application_is_a_404(client, admin_headers):
    """A stale link from a Slack message should say "gone", not 500."""
    resp = await client.get(
        f"/v1/admin/applications/{uuid.uuid4()}", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_malformed_application_id_is_rejected(client, admin_headers):
    """The path parameter is a UUID; anything else never reaches the database."""
    resp = await client.get("/v1/admin/applications/not-a-uuid", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── Advancing the pipeline ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_advancing_a_stage_appends_to_the_trail_rather_than_replacing_it(
    client, admin_headers, db_session
):
    """History is the point: "rejected" alone tells an applicant nothing, but
    submitted → interview → rejected is a story they can follow."""
    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    for stage in ("reviewing", "interview", "offer"):
        data = assert_envelope_ok(
            await client.patch(
                f"/v1/admin/applications/{application.id}/status",
                headers=admin_headers,
                json={"status": stage, "notify": False},
            )
        )
        assert data["status"] == stage

    assert [event["status"] for event in data["events"]] == [
        "reviewing",
        "interview",
        "offer",
    ]


@pytest.mark.asyncio
async def test_a_stage_that_is_not_in_the_pipeline_is_refused(
    client, admin_headers, db_session
):
    """Statuses drive the applicant-facing email copy, so a free-text stage
    would render as a message nobody wrote."""
    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    resp = await client.patch(
        f"/v1/admin/applications/{application.id}/status",
        headers=admin_headers,
        json={"status": "ghosted", "notify": False},
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_advancing_an_unknown_application_is_a_404(client, admin_headers):
    """Never create a pipeline row as a side effect of updating one."""
    resp = await client.patch(
        f"/v1/admin/applications/{uuid.uuid4()}/status",
        headers=admin_headers,
        json={"status": "reviewing", "notify": False},
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_notify_false_leaves_the_applicant_undisturbed(
    client, admin_headers, db_session, monkeypatch
):
    """Internal bookkeeping — moving someone to "reviewing" while you gather
    opinions — must not email them. The flag is the only thing standing
    between a private note and a message to a stranger's inbox."""
    sent: list[str] = []

    async def _capture(*args, **kwargs):
        sent.append(args[0] if args else "")
        return True

    monkeypatch.setattr(
        "app.services.portal.notifications.email_service.send_email", _capture
    )

    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/applications/{application.id}/status",
            headers=admin_headers,
            json={"status": "reviewing", "notify": False},
        )
    )
    assert sent == []
    assert data["events"][0]["notified"] is False


@pytest.mark.asyncio
async def test_a_failed_notification_still_records_the_decision(
    client, admin_headers, db_session, monkeypatch
):
    """Email is best-effort. If the provider is down, the stage change must
    still stick — losing a hiring decision because SMTP blipped is worse than
    an applicant not being emailed."""

    async def _boom(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.services.portal.notifications.email_service.send_email", _boom
    )

    job = await _job(db_session)
    application = await _application(db_session, job)
    await db_session.commit()

    data = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/applications/{application.id}/status",
            headers=admin_headers,
            json={"status": "hired", "notify": True},
        )
    )
    assert data["status"] == "hired"
    assert data["events"][0]["notified"] is False
