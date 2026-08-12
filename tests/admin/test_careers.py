"""Router tests for `/v1/careers` — the public hiring surface.

Applicants are not Loupe users, so every one of these routes is open to the
world. That shapes the rules worth pinning down: a role that isn't `open` must
be invisible (a draft posting is an internal document), an application can
only be tracked by someone who knows both its id *and* the email it was filed
under, and a failed guess must look exactly like a missing record so the
endpoint can't be used to enumerate applicants.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.career import JobPosting
from app.models.enums import EmploymentTypeEnum, JobStatusEnum
from app.schemas.portal import slugify
from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _make_job(
    db,
    *,
    title: str = "Backend Engineer",
    status: JobStatusEnum = JobStatusEnum.open,
) -> JobPosting:
    job = JobPosting(
        slug=slugify(title),
        title=title,
        team="Engineering",
        location="Remote (US)",
        employment_type=EmploymentTypeEnum.full_time.value,
        summary="Build the API behind the vault.",
        description="Long-form description.",
        status=status.value,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


def _application(**overrides) -> dict:
    payload = {
        "applicant_name": "Ada Lovelace",
        "applicant_email": "ada@example.com",
        "linkedin_url": "https://linkedin.com/in/ada",
        "cover_letter": "I like cards and compilers.",
    }
    payload.update(overrides)
    return payload


# ── RULE: only `open` roles exist as far as the public is concerned ──────


@pytest.mark.asyncio
async def test_the_jobs_board_lists_only_open_roles(client, db_session):
    """Drafts are internal working documents and closed roles are kept for
    record-keeping; neither should be discoverable by a candidate."""
    await _make_job(db_session, title="Backend Engineer")
    await _make_job(db_session, title="Designer", status=JobStatusEnum.draft)
    await _make_job(db_session, title="Recruiter", status=JobStatusEnum.closed)

    rows = assert_envelope_ok(await client.get("/v1/careers/jobs"))
    assert [r["title"] for r in rows] == ["Backend Engineer"]


@pytest.mark.asyncio
async def test_the_jobs_board_needs_no_account(client, db_session):
    """Requiring a Loupe login to read the careers page would rule out every
    candidate who isn't already a customer."""
    await _make_job(db_session)
    resp = await client.get("/v1/careers/jobs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_an_open_role_is_fetchable_by_slug(client, db_session):
    job = await _make_job(db_session)
    row = assert_envelope_ok(await client.get(f"/v1/careers/jobs/{job.slug}"))
    assert row["id"] == str(job.id)
    assert row["employment_type"] == "full_time"
    assert row["description"] == "Long-form description."


@pytest.mark.asyncio
async def test_a_draft_role_404s_by_slug(client, db_session):
    """Knowing the slug must not be enough — otherwise a guessable URL leaks
    a role before it is announced."""
    job = await _make_job(db_session, title="Designer", status=JobStatusEnum.draft)
    assert_envelope_error(
        await client.get(f"/v1/careers/jobs/{job.slug}"), expected_status=404
    )


@pytest.mark.asyncio
async def test_an_unknown_slug_404s(client):
    assert_envelope_error(
        await client.get("/v1/careers/jobs/chief-vibes-officer"), expected_status=404
    )


# ── Applying ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applying_returns_the_reference_the_candidate_tracks_with(
    client, db_session
):
    job = await _make_job(db_session)
    data = assert_envelope_ok(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=201,
    )
    assert data["status"] == "submitted"
    assert data["job_title"] == "Backend Engineer"
    assert uuid.UUID(data["id"])


@pytest.mark.asyncio
async def test_applying_opens_the_status_trail(client, db_session):
    """The event trail is the applicant's only view into the pipeline, so it
    has to start at submission rather than at the first admin action."""
    job = await _make_job(db_session)
    submitted = assert_envelope_ok(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=201,
    )
    tracked = assert_envelope_ok(
        await client.get(
            f"/v1/careers/applications/{submitted['id']}",
            params={"email": "ada@example.com"},
        )
    )
    assert [e["status"] for e in tracked["events"]] == ["submitted"]
    assert tracked["job_title"] == "Backend Engineer"
    assert "applicant_email" not in tracked, (
        "the tracking view is not an echo of the form"
    )


@pytest.mark.asyncio
async def test_an_applicant_email_is_normalised_so_tracking_survives_case(
    client, db_session
):
    """People type their address differently the second time. The address is
    the only key they have, so matching must not depend on shift keys."""
    job = await _make_job(db_session)
    submitted = assert_envelope_ok(
        await client.post(
            f"/v1/careers/jobs/{job.id}/apply",
            json=_application(applicant_email="Ada@Example.COM"),
        ),
        expected_status=201,
    )
    for typed in ("ada@example.com", "ADA@EXAMPLE.COM", "  Ada@Example.com  "):
        assert_envelope_ok(
            await client.get(
                f"/v1/careers/applications/{submitted['id']}", params={"email": typed}
            )
        )


@pytest.mark.asyncio
async def test_applying_to_a_draft_role_404s(client, db_session):
    """A role that isn't open cannot receive applications even if a candidate
    already has its id from an earlier link."""
    job = await _make_job(db_session, title="Designer", status=JobStatusEnum.draft)
    assert_envelope_error(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_applying_to_a_closed_role_404s(client, db_session):
    job = await _make_job(db_session, title="Recruiter", status=JobStatusEnum.closed)
    assert_envelope_error(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_applying_to_an_unknown_role_404s(client):
    assert_envelope_error(
        await client.post(
            f"/v1/careers/jobs/{uuid.uuid4()}/apply", json=_application()
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_applying_rejects_a_malformed_email(client, db_session):
    """The address is the applicant's only handle on the application; a typo
    that gets stored is an application nobody can ever be told about."""
    job = await _make_job(db_session)
    assert_envelope_error(
        await client.post(
            f"/v1/careers/jobs/{job.id}/apply",
            json=_application(applicant_email="not-an-email"),
        ),
        expected_status=422,
    )


@pytest.mark.asyncio
async def test_applications_are_rate_limited_per_client(client, db_session):
    """Applying is a rare, deliberate act, so the cap is tight — it is what
    stops an open, unauthenticated POST from becoming a spam funnel."""
    job = await _make_job(db_session)
    for _ in range(8):
        resp = await client.post(
            f"/v1/careers/jobs/{job.id}/apply", json=_application()
        )
        assert resp.status_code == 201

    blocked = await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application())
    assert_envelope_error(
        blocked, expected_status=429, expected_code="rate_limit.exceeded"
    )
    # A 429 without `Retry-After` tells a well-behaved client nothing about
    # when to come back, so it retries blindly — the envelope handler used to
    # rebuild the response without `exc.headers` and drop the limiter's header
    # on the floor. It has to survive the envelope.
    assert blocked.headers.get("Retry-After") == "300"


# ── Tracking ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tracking_with_the_wrong_email_looks_like_a_missing_record(
    client, db_session
):
    """404 rather than 403: a 'wrong email' answer would confirm the id is
    real, turning the endpoint into an application enumerator."""
    job = await _make_job(db_session)
    submitted = assert_envelope_ok(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=201,
    )
    assert_envelope_error(
        await client.get(
            f"/v1/careers/applications/{submitted['id']}",
            params={"email": "someone.else@example.com"},
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_tracking_an_unknown_application_404s(client):
    assert_envelope_error(
        await client.get(
            f"/v1/careers/applications/{uuid.uuid4()}",
            params={"email": "ada@example.com"},
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_tracking_without_an_email_is_rejected(client, db_session):
    """The email is the whole access check; an omitted one must never fall
    back to 'show it anyway'."""
    job = await _make_job(db_session)
    submitted = assert_envelope_ok(
        await client.post(f"/v1/careers/jobs/{job.id}/apply", json=_application()),
        expected_status=201,
    )
    assert_envelope_error(
        await client.get(f"/v1/careers/applications/{submitted['id']}"),
        expected_status=422,
    )
