"""How `GET /v1/careers/applications/{id}` accepts the applicant's email.

The email is not a filter on this route — it is the only access credential
guarding an application record. A credential in a query string is retained by
proxy access logs, browser history and any outbound ``Referer``, so the header
form is the supported channel and the query form is a deprecated compatibility
path for clients that have not migrated yet.

These live in ``tests/public`` rather than beside the rest of the careers
router tests because they are about the public credential channel, not the
hiring pipeline.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.career import JobPosting
from app.models.enums import EmploymentTypeEnum, JobStatusEnum
from app.schemas.portal import slugify
from tests.conftest import assert_envelope_error, assert_envelope_ok

HEADER = "X-Applicant-Email"


async def _make_job(db, *, title: str = "Backend Engineer") -> JobPosting:
    job = JobPosting(
        slug=slugify(title),
        title=title,
        team="Engineering",
        location="Remote (US)",
        employment_type=EmploymentTypeEnum.full_time.value,
        summary="Build the API behind the vault.",
        description="Long-form description.",
        status=JobStatusEnum.open.value,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def _apply(client, job, email: str = "ada@example.com") -> str:
    submitted = assert_envelope_ok(
        await client.post(
            f"/v1/careers/jobs/{job.id}/apply",
            json={
                "applicant_name": "Ada Lovelace",
                "applicant_email": email,
                "cover_letter": "I like cards and compilers.",
            },
        ),
        expected_status=201,
    )
    return submitted["id"]


@pytest.mark.asyncio
async def test_the_credential_can_travel_in_a_header_instead_of_the_url(
    client, db_session
):
    """The supported channel: nothing about the request that a proxy writes to
    an access log carries the applicant's address."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    tracked = assert_envelope_ok(
        await client.get(
            f"/v1/careers/applications/{application_id}",
            headers={HEADER: "ada@example.com"},
        )
    )
    assert tracked["job_title"] == "Backend Engineer"
    assert [e["status"] for e in tracked["events"]] == ["submitted"]


@pytest.mark.asyncio
async def test_the_header_is_held_to_the_same_check_as_the_query_form(
    client, db_session
):
    """A cheaper-to-send credential must not be a weaker one: a wrong address
    in the header still looks exactly like a missing record."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    assert_envelope_error(
        await client.get(
            f"/v1/careers/applications/{application_id}",
            headers={HEADER: "someone.else@example.com"},
        ),
        expected_status=404,
    )
    assert_envelope_error(
        await client.get(
            f"/v1/careers/applications/{uuid.uuid4()}",
            headers={HEADER: "ada@example.com"},
        ),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_the_header_is_normalised_like_the_query_form(client, db_session):
    """Case and stray whitespace are the applicant's, not a different person's
    — the header goes through the same normalisation the query form gets."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job, email="Ada@Example.COM")

    for typed in ("ada@example.com", "ADA@EXAMPLE.COM", "  Ada@Example.com  "):
        assert_envelope_ok(
            await client.get(
                f"/v1/careers/applications/{application_id}", headers={HEADER: typed}
            )
        )


@pytest.mark.asyncio
async def test_the_deprecated_query_form_still_works_for_existing_clients(
    client, db_session
):
    """The emailed tracking link and the shipped web client still spell the
    credential as `?email=`, so it stays supported until they migrate."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    assert_envelope_ok(
        await client.get(
            f"/v1/careers/applications/{application_id}",
            params={"email": "ada@example.com"},
        )
    )


@pytest.mark.asyncio
async def test_the_header_wins_when_both_are_sent(client, db_session):
    """A client mid-migration may send both. The header is the channel we
    trust, so it decides — a stale query param cannot override it."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    assert_envelope_ok(
        await client.get(
            f"/v1/careers/applications/{application_id}",
            params={"email": "someone.else@example.com"},
            headers={HEADER: "ada@example.com"},
        )
    )


@pytest.mark.asyncio
async def test_a_credentialed_url_is_never_cached(client, db_session):
    """When the deprecated query form is used the URL itself is a secret, so
    the response must not be retained by a shared or on-disk cache."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    resp = await client.get(
        f"/v1/careers/applications/{application_id}",
        params={"email": "ada@example.com"},
    )
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_neither_channel_means_rejected_not_shown_anyway(client, db_session):
    """The email is the whole access check; omitting it from both channels
    must never fall back to 'show it anyway'."""
    job = await _make_job(db_session)
    application_id = await _apply(client, job)

    for request_kwargs in ({}, {"headers": {HEADER: "   "}}, {"params": {"email": ""}}):
        assert_envelope_error(
            await client.get(
                f"/v1/careers/applications/{application_id}", **request_kwargs
            ),
            expected_status=422,
        )
