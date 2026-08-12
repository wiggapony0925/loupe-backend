"""Router tests for /v1/admin/jobs — the careers board's write surface.

A job posting is the only admin-authored record that turns into a public web
page, so three things carry the weight here: the admin gate, the slug that
becomes the public URL, and the audit trail behind every mutation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.models.audit import AuditLog
from app.models.career import JobPosting
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


def _payload(**over) -> dict:
    """A minimal valid posting; override any field per test."""
    body = {
        "title": "Staff Backend Engineer",
        "team": "Platform",
        "location": "Remote (US)",
        "summary": "Own the pricing pipeline end to end.",
    }
    body.update(over)
    return body


async def _create(client, headers, **over) -> dict:
    resp = await client.post("/v1/admin/jobs", json=_payload(**over), headers=headers)
    return assert_envelope_ok(resp, expected_status=201)


# ── Authorization ───────────────────────────────────────────────────────────

_ROUTES = (
    ("get", "/v1/admin/jobs"),
    ("post", "/v1/admin/jobs"),
    ("get", f"/v1/admin/jobs/{uuid.uuid4()}"),
    ("patch", f"/v1/admin/jobs/{uuid.uuid4()}"),
    ("delete", f"/v1/admin/jobs/{uuid.uuid4()}"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _ROUTES)
async def test_job_routes_reject_anonymous_callers(client, method, path):
    """No token is a 401 — including on the read routes, because the admin
    list carries drafts that are deliberately not public yet."""
    resp = await getattr(client, method)(path)
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _ROUTES)
async def test_job_routes_reject_ordinary_users(client, auth_headers, method, path):
    """Being signed in is not enough: anyone could otherwise publish a role on
    the company careers page."""
    resp = await getattr(client, method)(path, headers=auth_headers)
    assert_envelope_error(resp, expected_status=403)


# ── POST /v1/admin/jobs ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_new_posting_starts_as_an_unpublished_draft(client, admin_headers):
    """Creating a role must never publish it. The public careers page lists
    only ``open`` postings, so the safe default is ``draft`` — an admin
    publishes deliberately, with a second request."""
    body = await _create(client, admin_headers)

    assert body["status"] == "draft"
    assert body["employment_type"] == "full_time"
    assert body["title"] == "Staff Backend Engineer"


@pytest.mark.asyncio
async def test_the_public_slug_is_derived_from_the_title(client, admin_headers):
    """The slug is the public URL for the role, so it is generated rather than
    trusted from the client when omitted."""
    body = await _create(client, admin_headers, title="Senior iOS Engineer (Scanner)")
    assert body["slug"] == "senior-ios-engineer-scanner"


@pytest.mark.asyncio
async def test_two_postings_with_the_same_title_get_distinct_slugs(
    client, admin_headers
):
    """Re-opening last quarter's role must not collide with the archived one —
    the slug is unique in the database, so a clash would otherwise 500."""
    first = await _create(client, admin_headers, title="Support Engineer")
    second = await _create(client, admin_headers, title="Support Engineer")

    assert first["slug"] == "support-engineer"
    assert second["slug"] == "support-engineer-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"title": "X"},  # below the 2-char minimum
        {"team": ""},  # every posting must name a team
        {"summary": ""},  # the summary is the careers-page teaser
        {"employment_type": "freelance"},  # not in the enum
        {"status": "published"},  # the lifecycle is draft/open/closed
    ],
)
async def test_a_posting_must_be_publishable_to_be_saved(client, admin_headers, bad):
    """The stored row feeds a public page directly, so the shape is validated
    on the way in rather than patched up at render time."""
    resp = await client.post(
        "/v1/admin/jobs", json=_payload(**bad), headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_creating_a_posting_is_written_to_the_audit_trail(
    client, admin_headers, admin_user, db_session
):
    """Careers content is externally visible, so 'who put this on the site'
    must be answerable after the fact."""
    body = await _create(client, admin_headers)

    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.create")
        )
    ).scalar_one()
    assert row.user_id == admin_user.id
    assert row.target_table == "job_postings"
    assert row.target_id == body["id"]
    assert row.payload == {"title": body["title"], "status": "draft"}


# ── GET /v1/admin/jobs ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_admin_list_shows_drafts_and_closed_roles(client, admin_headers):
    """This is the editing surface, not the careers page: a draft nobody can
    see publicly still has to be findable by the person writing it."""
    await _create(client, admin_headers, title="Draft Role")
    await _create(client, admin_headers, title="Live Role", status="open")
    await _create(client, admin_headers, title="Archived Role", status="closed")

    body = assert_envelope_ok(await client.get("/v1/admin/jobs", headers=admin_headers))

    assert {j["status"] for j in body} == {"draft", "open", "closed"}


@pytest.mark.asyncio
async def test_the_admin_list_is_newest_first(client, admin_headers, db_session):
    """Hiring work happens at the top of the list — the role someone just
    opened should not be buried under years of archived postings."""
    now = datetime.now(UTC)
    db_session.add_all(
        [
            JobPosting(
                slug="older",
                title="Older Role",
                team="Platform",
                location="Remote",
                employment_type="full_time",
                summary="s",
                description="",
                status="open",
                created_at=now - timedelta(days=10),
                updated_at=now - timedelta(days=10),
            ),
            JobPosting(
                slug="newer",
                title="Newer Role",
                team="Platform",
                location="Remote",
                employment_type="full_time",
                summary="s",
                description="",
                status="open",
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(await client.get("/v1/admin/jobs", headers=admin_headers))
    assert [j["slug"] for j in body] == ["newer", "older"]


@pytest.mark.asyncio
async def test_an_empty_board_lists_nothing_rather_than_erroring(client, admin_headers):
    assert (
        assert_envelope_ok(await client.get("/v1/admin/jobs", headers=admin_headers))
        == []
    )


# ── GET /v1/admin/jobs/{job_id} ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetching_a_posting_returns_the_editable_record(client, admin_headers):
    created = await _create(client, admin_headers, description="# About the role")

    body = assert_envelope_ok(
        await client.get(f"/v1/admin/jobs/{created['id']}", headers=admin_headers)
    )
    assert body["id"] == created["id"]
    assert body["description"] == "# About the role"


@pytest.mark.asyncio
async def test_fetching_a_posting_that_does_not_exist_is_404(client, admin_headers):
    resp = await client.get(f"/v1/admin/jobs/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_malformed_job_id_is_rejected_before_the_lookup(client, admin_headers):
    """The path is typed as a UUID, so garbage is a 422 (bad request), never a
    404 that would imply the id was merely unknown."""
    resp = await client.get("/v1/admin/jobs/not-a-uuid", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── PATCH /v1/admin/jobs/{job_id} ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_patch_only_touches_the_fields_it_sends(client, admin_headers):
    """Partial update: the portal's inline edits send one field, and the rest
    of the posting must survive untouched."""
    created = await _create(client, admin_headers, description="Long description")

    body = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/jobs/{created['id']}",
            json={"location": "London, UK"},
            headers=admin_headers,
        )
    )
    assert body["location"] == "London, UK"
    assert body["title"] == created["title"]
    assert body["description"] == "Long description"
    assert body["slug"] == created["slug"]


@pytest.mark.asyncio
async def test_publishing_a_posting_is_a_status_change(client, admin_headers):
    """Going live is the same record flipping to ``open`` — the public page
    reads status, so nothing else needs to move."""
    created = await _create(client, admin_headers)

    body = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/jobs/{created['id']}",
            json={"status": "open"},
            headers=admin_headers,
        )
    )
    assert body["status"] == "open"


@pytest.mark.asyncio
async def test_a_renamed_slug_stays_unique_against_other_postings(
    client, admin_headers
):
    """Editing a slug goes through the same uniqueness pass as creating one,
    so two roles can never fight over a public URL."""
    await _create(client, admin_headers, title="Data Engineer")
    other = await _create(client, admin_headers, title="Placeholder")

    body = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/jobs/{other['id']}",
            json={"slug": "data-engineer"},
            headers=admin_headers,
        )
    )
    assert body["slug"] == "data-engineer-2"


@pytest.mark.asyncio
async def test_patching_records_only_the_changed_fields_in_the_audit_payload(
    client, admin_headers, db_session
):
    """The audit payload is a diff, not a snapshot — it should read as 'they
    closed this role', not as a wall of unchanged text."""
    created = await _create(client, admin_headers)
    await client.patch(
        f"/v1/admin/jobs/{created['id']}",
        json={"status": "closed"},
        headers=admin_headers,
    )

    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.update")
        )
    ).scalar_one()
    assert row.target_id == created["id"]
    assert row.payload == {"status": "closed"}


@pytest.mark.asyncio
async def test_patching_a_posting_that_does_not_exist_is_404(client, admin_headers):
    resp = await client.patch(
        f"/v1/admin/jobs/{uuid.uuid4()}",
        json={"status": "open"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_patching_a_required_field_to_null_is_rejected(client, admin_headers):
    """A posting has no "no title" state — title/team/location/summary and the
    two enums are all NOT NULL. The fields are optional so a patch may *omit*
    them; sending an explicit null is a client error and is refused as a 422
    rather than carried down to the database.
    """
    created = await _create(client, admin_headers)

    for field in (
        "title",
        "team",
        "location",
        "summary",
        "description",
        "employment_type",
        "status",
    ):
        resp = await client.patch(
            f"/v1/admin/jobs/{created['id']}",
            json={field: None},
            headers=admin_headers,
        )
        assert_envelope_error(resp, expected_status=422)

    # …and the posting is untouched by any of it.
    body = assert_envelope_ok(
        await client.get(f"/v1/admin/jobs/{created['id']}", headers=admin_headers)
    )
    assert body["title"] == created["title"]


@pytest.mark.asyncio
async def test_a_patch_that_omits_a_field_leaves_it_alone(client, admin_headers):
    """Refusing explicit nulls must not turn a partial update into a full one:
    an unmentioned field keeps its value."""
    created = await _create(client, admin_headers)

    body = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/jobs/{created['id']}",
            json={"team": "Infrastructure"},
            headers=admin_headers,
        )
    )
    assert body["team"] == "Infrastructure"
    assert body["title"] == created["title"]
    assert body["location"] == created["location"]
    assert body["summary"] == created["summary"]


@pytest.mark.asyncio
async def test_a_patch_is_validated_like_a_create(client, admin_headers):
    created = await _create(client, admin_headers)
    resp = await client.patch(
        f"/v1/admin/jobs/{created['id']}",
        json={"employment_type": "freelance"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


# ── DELETE /v1/admin/jobs/{job_id} ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_posting_takes_it_off_the_board(client, admin_headers):
    """Deletion is real, not a soft flag — a 204 with no body, and the record
    is gone on the next read."""
    created = await _create(client, admin_headers)

    resp = await client.delete(f"/v1/admin/jobs/{created['id']}", headers=admin_headers)
    assert resp.status_code == 204
    assert resp.content == b""

    follow_up = await client.get(
        f"/v1/admin/jobs/{created['id']}", headers=admin_headers
    )
    assert_envelope_error(follow_up, expected_status=404)


@pytest.mark.asyncio
async def test_deleting_a_posting_is_written_to_the_audit_trail(
    client, admin_headers, admin_user, db_session
):
    """A destructive action on public content is exactly what the trail is
    for: the row survives the posting it describes."""
    created = await _create(client, admin_headers)
    await client.delete(f"/v1/admin/jobs/{created['id']}", headers=admin_headers)

    row = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "job.delete")
        )
    ).scalar_one()
    assert row.user_id == admin_user.id
    assert row.target_table == "job_postings"
    assert row.target_id == created["id"]


@pytest.mark.asyncio
async def test_deleting_a_posting_that_does_not_exist_is_404(client, admin_headers):
    """A double-click on Delete gets a clean 404, not a 204 that would imply
    something was removed."""
    resp = await client.delete(f"/v1/admin/jobs/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)
