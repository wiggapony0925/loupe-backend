"""Admin user management (`/v1/admin/users`) — the privilege surface.

Every route in this router can hand out, or take away, access to the whole
product, so these tests are weighted toward the boundary rather than the
payload. Three things are being pinned down:

1. **The gate holds on every route.** Anonymous is challenged (401), an
   ordinary signed-in account is refused (403). A single missing
   `require_admin` here is a total compromise, and the router relies on one
   subtree-level dependency to cover routes that never mention it.
2. **A mutation is only real if it changes what the user can DO.** A
   `banned: true` field in a response is a claim; a 403 on that user's very
   next request is the proof. Ban, unban, role and delete are all asserted
   that way.
3. **The safety rails hold.** A super-admin (an `ADMIN_EMAILS` address) can
   never be demoted, banned or deleted, and an admin can't do any of those
   to their own account — so there is always a way back in.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import select

from app.auth.jwt import issue_token
from app.config import get_settings
from app.models.audit import AuditLog
from app.models.user import User
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@contextlib.contextmanager
def _super_admins(emails: str):
    """Temporarily populate the `ADMIN_EMAILS` allowlist.

    Super-admin status is derived from an env allowlist rather than a column,
    so the only way to build one in a test is to edit the setting.
    """
    settings = get_settings()
    previous = settings.admin_emails
    settings.admin_emails = emails  # type: ignore[misc]
    try:
        yield
    finally:
        settings.admin_emails = previous  # type: ignore[misc]


def _headers(user: User) -> dict[str, str]:
    token, _ = issue_token(user.id, "access", {"ver": user.token_version})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_user(db_session):
    """An ordinary staff operator: a DB-backed admin grant, NOT a super-admin.

    Deliberately not in `ADMIN_EMAILS` — this is the account the portal is
    actually used from, and the one whose limits (can't ban itself, can't
    touch a super-admin) are worth proving.
    """
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    return _headers(admin_user)


# ── RULE: the whole router is admin-only ──
#
# Enumerated rather than spot-checked. `GET /users` and `GET /users/{id}` do
# not declare `require_admin` themselves — they inherit it from the router —
# so a refactor that moved a route out of the subtree would silently expose
# the entire user table. The `{uid}` target is a real, ordinary user, so a
# refusal can only come from the gate and never from a 404.

ROUTES = [
    ("GET", "/v1/admin/users", None),
    ("GET", "/v1/admin/users/{uid}", None),
    ("POST", "/v1/admin/users/test", None),
    ("PATCH", "/v1/admin/users/{uid}/role", {"is_admin": True}),
    ("PATCH", "/v1/admin/users/{uid}/plan", {"plan": "pro"}),
    ("POST", "/v1/admin/users/{uid}/ban", {"reason": "spam"}),
    ("POST", "/v1/admin/users/{uid}/unban", None),
    ("DELETE", "/v1/admin/users/{uid}", None),
]

ROUTE_IDS = [f"{method}-{template}" for method, template, _ in ROUTES]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template", "body"), ROUTES, ids=ROUTE_IDS)
async def test_admin_user_routes_challenge_an_anonymous_caller(
    client, second_user, method, template, body
):
    resp = await client.request(method, template.format(uid=second_user.id), json=body)
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "template", "body"), ROUTES, ids=ROUTE_IDS)
async def test_admin_user_routes_refuse_an_ordinary_signed_in_user(
    client, auth_headers, second_user, method, template, body
):
    """Signed in is not the same as privileged — this is the escalation path
    an attacker with any account would try first."""
    resp = await client.request(
        method,
        template.format(uid=second_user.id),
        headers=auth_headers,
        json=body,
    )
    assert_envelope_error(resp, expected_status=403)


# ── Listing and searching ──


@pytest.mark.asyncio
async def test_the_user_list_finds_an_account_by_an_email_fragment(
    client, admin_headers, db_session
):
    await make_user(db_session, email="findme+needle@example.com")
    await make_user(db_session, email="somebody-else@example.com")

    page = assert_envelope_ok(
        await client.get("/v1/admin/users?q=needle", headers=admin_headers)
    )
    assert [row["email"] for row in page["results"]] == ["findme+needle@example.com"]
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_the_user_list_search_is_case_insensitive(
    client, admin_headers, db_session
):
    """Support staff paste an address out of an email client, capitals and
    all; a case-sensitive search would tell them the account doesn't exist."""
    await make_user(db_session, email="mixedcase+hunted@example.com")

    page = assert_envelope_ok(
        await client.get("/v1/admin/users?q=HUNTED", headers=admin_headers)
    )
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_the_user_list_reports_the_total_beyond_the_current_page(
    client, admin_headers, db_session, created_user, second_user
):
    """`total` counts matches, not rows returned — the pager needs the size of
    the whole result set to know there is a page two."""
    page = assert_envelope_ok(
        await client.get("/v1/admin/users?page=1&page_size=1", headers=admin_headers)
    )
    assert len(page["results"]) == 1
    assert page["page_size"] == 1
    assert page["total"] >= 3  # created_user, second_user, the admin


@pytest.mark.asyncio
async def test_the_user_list_rejects_an_oversized_page_size(client, admin_headers):
    """The cap is a guard against someone dumping the user table in one
    request, so it is a validation error rather than a silent clamp."""
    resp = await client.get("/v1/admin/users?page_size=5000", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_the_user_list_badges_a_super_admin(
    client, admin_headers, db_session, created_user
):
    """`is_super_admin` is computed from the env allowlist, not stored, so the
    portal can grey out the actions that would fail anyway."""
    with _super_admins(created_user.email):
        page = assert_envelope_ok(
            await client.get(
                "/v1/admin/users",
                params={"q": created_user.email},
                headers=admin_headers,
            )
        )
    assert page["results"][0]["is_super_admin"] is True


# ── The detail drawer ──


@pytest.mark.asyncio
async def test_user_detail_reports_how_the_account_signs_in(
    client, admin_headers, created_user
):
    """Support's first question on a "can't log in" ticket is which button the
    user is supposed to be pressing."""
    detail = assert_envelope_ok(
        await client.get(f"/v1/admin/users/{created_user.id}", headers=admin_headers)
    )
    assert detail["auth_method"] == "apple"  # the fixture has an apple_subject
    assert detail["email"] == created_user.email


@pytest.mark.asyncio
async def test_user_detail_starts_every_activity_count_at_zero(
    client, admin_headers, created_user
):
    """The aggregates are separate queries; a fresh account proves they run
    and return a number rather than null."""
    detail = assert_envelope_ok(
        await client.get(f"/v1/admin/users/{created_user.id}", headers=admin_headers)
    )
    assert detail["grades_count"] == 0
    assert detail["watchlist_count"] == 0
    assert detail["scans_count"] == 0
    assert detail["estimated_value_usd"] == 0.0


@pytest.mark.asyncio
async def test_user_detail_404s_for_an_id_that_does_not_exist(client, admin_headers):
    resp = await client.get(f"/v1/admin/users/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_user_detail_422s_for_an_id_that_is_not_a_uuid(client, admin_headers):
    resp = await client.get("/v1/admin/users/not-a-uuid", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── Sandbox accounts ──


@pytest.mark.asyncio
async def test_a_minted_test_account_can_sign_in_with_the_password_returned(
    client, admin_headers
):
    """The password is shown exactly once and never recoverable, so if it
    doesn't work the account is landfill."""
    created = assert_envelope_ok(
        await client.post("/v1/admin/users/test", headers=admin_headers),
        expected_status=201,
    )

    login = assert_envelope_ok(
        await client.post(
            "/v1/auth/login",
            json={"email": created["email"], "password": created["password"]},
        )
    )
    me = assert_envelope_ok(
        await client.get(
            "/v1/me",
            headers={"Authorization": f"Bearer {login['access_token']}"},
        )
    )
    assert me["email"] == created["email"]


@pytest.mark.asyncio
async def test_two_test_accounts_do_not_collide(client, admin_headers):
    """The email is random; minting twice in a row must not trip the unique
    constraint and 500 the portal button."""
    first = assert_envelope_ok(
        await client.post("/v1/admin/users/test", headers=admin_headers),
        expected_status=201,
    )
    second = assert_envelope_ok(
        await client.post("/v1/admin/users/test", headers=admin_headers),
        expected_status=201,
    )
    assert first["email"] != second["email"]
    assert first["id"] != second["id"]


# ── Role: granting and revoking admin ──


@pytest.mark.asyncio
async def test_granting_admin_opens_the_portal_to_that_user(
    client, admin_headers, second_user, second_user_headers
):
    """The assertion that matters is not the flag in the response but that the
    promoted account can now reach an admin-only route."""
    assert_envelope_error(
        await client.get("/v1/admin/users", headers=second_user_headers),
        expected_status=403,
    )

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/users/{second_user.id}/role",
            json={"is_admin": True},
            headers=admin_headers,
        )
    )
    assert updated["is_admin"] is True

    assert_envelope_ok(await client.get("/v1/admin/users", headers=second_user_headers))


@pytest.mark.asyncio
async def test_revoking_admin_closes_the_portal_again(
    client, admin_headers, db_session, second_user, second_user_headers
):
    second_user.is_admin = True
    await db_session.commit()
    assert_envelope_ok(await client.get("/v1/admin/users", headers=second_user_headers))

    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/users/{second_user.id}/role",
            json={"is_admin": False},
            headers=admin_headers,
        )
    )
    assert updated["is_admin"] is False

    assert_envelope_error(
        await client.get("/v1/admin/users", headers=second_user_headers),
        expected_status=403,
    )


@pytest.mark.asyncio
async def test_an_admin_cannot_change_their_own_role(client, admin_user, admin_headers):
    """Self-demotion is how an operator locks themselves out of the portal
    with no way back short of a DB console."""
    resp = await client.patch(
        f"/v1/admin/users/{admin_user.id}/role",
        json={"is_admin": False},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_a_super_admin_cannot_be_demoted(
    client, admin_headers, created_user, db_session
):
    """`ADMIN_EMAILS` is the bootstrap floor: if it could be toggled off from
    the portal, a compromised staff account could lock out the owners."""
    target_id = created_user.id
    with _super_admins(created_user.email):
        resp = await client.patch(
            f"/v1/admin/users/{target_id}/role",
            json={"is_admin": False},
            headers=admin_headers,
        )
        assert_envelope_error(resp, expected_status=403)

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.is_admin is False  # untouched, not flipped-then-refused


@pytest.mark.asyncio
async def test_setting_a_role_404s_for_an_unknown_user(client, admin_headers):
    resp = await client.patch(
        f"/v1/admin/users/{uuid.uuid4()}/role",
        json={"is_admin": True},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_setting_a_role_requires_the_flag_in_the_body(
    client, admin_headers, second_user
):
    """`is_admin` has no default — an empty body must not be read as "revoke"
    (or, worse, as "grant")."""
    resp = await client.patch(
        f"/v1/admin/users/{second_user.id}/role", json={}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


# ── Plan: comping a user to Loupe Pro ──


@pytest.mark.asyncio
async def test_comping_a_user_to_pro_stamps_pro_since_and_never_expires(
    client, admin_headers, db_session, second_user
):
    """A comp is a lifetime grant until an operator changes it — an expiry
    would silently drop a tester back to free mid-test."""
    target_id = second_user.id
    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/users/{target_id}/plan",
            json={"plan": "pro"},
            headers=admin_headers,
        )
    )
    assert updated["plan"] == "pro"
    assert updated["pro_expires_at"] is None
    assert updated["has_subscription"] is False  # a comp, not a Stripe sub

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.pro_since is not None


@pytest.mark.asyncio
async def test_moving_a_comped_user_back_to_free_clears_the_expiry(
    client, admin_headers, db_session, second_user
):
    await client.patch(
        f"/v1/admin/users/{second_user.id}/plan",
        json={"plan": "pro"},
        headers=admin_headers,
    )
    updated = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/users/{second_user.id}/plan",
            json={"plan": "free"},
            headers=admin_headers,
        )
    )
    assert updated["plan"] == "free"
    assert updated["pro_expires_at"] is None


@pytest.mark.asyncio
async def test_the_plan_field_accepts_only_free_or_pro(
    client, admin_headers, second_user
):
    """The plan string is written straight onto the user row, so the allowed
    set is enforced at the schema — a typo like "Pro" must not become a plan
    nobody's entitlement check recognises."""
    resp = await client.patch(
        f"/v1/admin/users/{second_user.id}/plan",
        json={"plan": "enterprise"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_an_admin_cannot_comp_their_own_account_to_pro(
    client, admin_user, admin_headers, db_session
):
    """A comp is a paid entitlement, so it takes a second party — the same rail
    role, ban and delete already hold. Self-service Pro would let any
    DB-granted admin award themselves a lifetime subscription unobserved.
    """
    admin_id = admin_user.id
    resp = await client.patch(
        f"/v1/admin/users/{admin_id}/plan",
        json={"plan": "pro"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=400)

    db_session.expire_all()
    refreshed = await db_session.get(User, admin_id)
    assert refreshed is not None
    assert refreshed.plan == "free"  # untouched, not granted-then-refused


@pytest.mark.asyncio
async def test_a_super_admins_plan_cannot_be_changed(
    client, admin_headers, created_user, db_session
):
    """Protected accounts are protected on every mutating action, plan
    included — a staff admin must not be able to strip an owner's Pro."""
    target_id = created_user.id
    created_user.plan = "pro"
    await db_session.commit()

    with _super_admins(created_user.email):
        resp = await client.patch(
            f"/v1/admin/users/{target_id}/plan",
            json={"plan": "free"},
            headers=admin_headers,
        )
        assert_envelope_error(resp, expected_status=403)

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.plan == "pro"


@pytest.mark.asyncio
async def test_dropping_a_paying_subscriber_to_free_is_refused(
    client, admin_headers, db_session, second_user
):
    """This route writes the `plan` column and nothing else, so using it on a
    live Stripe subscriber would leave the subscription billing while the
    account read as free. Cancelling is its own super-admin route, which tells
    Stripe first and lets the webhook do the downgrade; this one refuses rather
    than desyncing the two.
    """
    target_id = second_user.id
    second_user.plan = "pro"
    second_user.stripe_subscription_id = "sub_live_123"
    await db_session.commit()

    resp = await client.patch(
        f"/v1/admin/users/{target_id}/plan",
        json={"plan": "free"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=409)

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.plan == "pro"  # still entitled, still billed — consistent
    assert refreshed.stripe_subscription_id == "sub_live_123"


@pytest.mark.asyncio
async def test_a_churned_subscriber_can_still_be_comped_to_pro(
    client, admin_headers, db_session, second_user
):
    """The refusal is aimed at taking Pro away from an account Stripe is still
    billing — it must not stop support *giving* Pro to someone whose
    subscription has already lapsed (the webhook downgraded them to free but
    left the subscription id on the row).
    """
    target_id = second_user.id
    second_user.plan = "free"  # webhook already reconciled the cancellation
    second_user.stripe_subscription_id = "sub_dead_123"
    await db_session.commit()

    comped = assert_envelope_ok(
        await client.patch(
            f"/v1/admin/users/{target_id}/plan",
            json={"plan": "pro"},
            headers=admin_headers,
        )
    )
    assert comped["plan"] == "pro"
    assert comped["pro_expires_at"] is None


@pytest.mark.asyncio
async def test_setting_a_plan_404s_for_an_unknown_user(client, admin_headers):
    resp = await client.patch(
        f"/v1/admin/users/{uuid.uuid4()}/plan",
        json={"plan": "pro"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=404)


# ── Ban ──


@pytest.mark.asyncio
async def test_banning_a_user_blocks_their_very_next_request(
    client, admin_headers, second_user, second_user_headers
):
    """**The** test for this surface. A ban that only sets a column is not a
    ban: the already-issued token in the banned user's pocket has to stop
    working, without waiting for it to expire."""
    assert_envelope_ok(await client.get("/v1/me", headers=second_user_headers))

    assert_envelope_ok(
        await client.post(
            f"/v1/admin/users/{second_user.id}/ban",
            json={"reason": "listing counterfeits"},
            headers=admin_headers,
        )
    )

    assert_envelope_error(
        await client.get("/v1/me", headers=second_user_headers), expected_status=403
    )


@pytest.mark.asyncio
async def test_a_ban_records_the_reason_and_the_moment(
    client, admin_headers, second_user
):
    """The reason is what a support agent reads back to the user, and what an
    appeal is judged against."""
    banned = assert_envelope_ok(
        await client.post(
            f"/v1/admin/users/{second_user.id}/ban",
            json={"reason": "listing counterfeits"},
            headers=admin_headers,
        )
    )
    assert banned["banned"] is True
    assert banned["ban_reason"] == "listing counterfeits"
    assert banned["banned_at"] is not None


@pytest.mark.asyncio
async def test_a_ban_with_a_blank_reason_stores_no_reason_at_all(
    client, admin_headers, second_user
):
    """Whitespace is normalised to NULL so the drawer shows "no reason given"
    rather than an empty quote that looks like a rendering bug."""
    banned = assert_envelope_ok(
        await client.post(
            f"/v1/admin/users/{second_user.id}/ban",
            json={"reason": "   "},
            headers=admin_headers,
        )
    )
    assert banned["ban_reason"] is None


@pytest.mark.asyncio
async def test_an_admin_cannot_ban_their_own_account(client, admin_user, admin_headers):
    """Self-ban would lock the operator out with the same token they'd need to
    undo it."""
    resp = await client.post(
        f"/v1/admin/users/{admin_user.id}/ban", json={}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_a_super_admin_cannot_be_banned(
    client, admin_headers, created_user, db_session
):
    target_id = created_user.id
    with _super_admins(created_user.email):
        resp = await client.post(
            f"/v1/admin/users/{target_id}/ban",
            json={"reason": "nice try"},
            headers=admin_headers,
        )
        assert_envelope_error(resp, expected_status=403)

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.banned_at is None


@pytest.mark.asyncio
async def test_banning_404s_for_an_unknown_user(client, admin_headers):
    resp = await client.post(
        f"/v1/admin/users/{uuid.uuid4()}/ban", json={}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_ban_reason_longer_than_the_column_is_rejected(
    client, admin_headers, second_user
):
    """Bounded at the schema rather than truncated in the DB — a silently
    clipped reason is a record that no longer says what it said."""
    resp = await client.post(
        f"/v1/admin/users/{second_user.id}/ban",
        json={"reason": "x" * 501},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


# ── Unban ──


@pytest.mark.asyncio
async def test_unbanning_gives_the_account_back(
    client, admin_headers, second_user, second_user_headers
):
    """A ban has to be fully reversible — the mistaken-ban path is the one
    support walks most often."""
    await client.post(
        f"/v1/admin/users/{second_user.id}/ban",
        json={"reason": "mistake"},
        headers=admin_headers,
    )
    assert_envelope_error(
        await client.get("/v1/me", headers=second_user_headers), expected_status=403
    )

    restored = assert_envelope_ok(
        await client.post(
            f"/v1/admin/users/{second_user.id}/unban", headers=admin_headers
        )
    )
    assert restored["banned"] is False
    assert restored["ban_reason"] is None

    assert_envelope_ok(await client.get("/v1/me", headers=second_user_headers))


@pytest.mark.asyncio
async def test_unbanning_an_account_that_was_never_banned_is_a_no_op(
    client, admin_headers, second_user
):
    """Idempotent on purpose: the portal button is enabled from a list row
    that may be stale, and a double-tap must not be an error."""
    restored = assert_envelope_ok(
        await client.post(
            f"/v1/admin/users/{second_user.id}/unban", headers=admin_headers
        )
    )
    assert restored["banned"] is False


@pytest.mark.asyncio
async def test_unbanning_404s_for_an_unknown_user(client, admin_headers):
    resp = await client.post(
        f"/v1/admin/users/{uuid.uuid4()}/unban", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


# ── Delete (soft) ──


@pytest.mark.asyncio
async def test_deleting_a_user_ends_their_session_but_keeps_the_row(
    client, admin_headers, db_session, second_user, second_user_headers
):
    """Soft delete: the account stops working immediately, and the row stays
    for the audit trail, for billing history, and so the deletion can be
    undone if it was a mistake."""
    target_id = second_user.id
    resp = await client.delete(f"/v1/admin/users/{target_id}", headers=admin_headers)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""  # 204 carries no envelope

    assert_envelope_error(
        await client.get("/v1/me", headers=second_user_headers), expected_status=401
    )

    db_session.expire_all()
    row = await db_session.get(User, target_id)
    assert row is not None
    assert row.deleted_at is not None


@pytest.mark.asyncio
async def test_a_deleted_account_is_still_listed_and_flagged(
    client, admin_headers, second_user
):
    """Deleted users stay in the table — an operator investigating an incident
    needs to find the account, not discover it has vanished."""
    email = second_user.email
    await client.delete(f"/v1/admin/users/{second_user.id}", headers=admin_headers)

    page = assert_envelope_ok(
        await client.get("/v1/admin/users", params={"q": email}, headers=admin_headers)
    )
    assert page["results"][0]["deleted"] is True


@pytest.mark.asyncio
async def test_an_admin_cannot_delete_their_own_account(
    client, admin_user, admin_headers
):
    resp = await client.delete(
        f"/v1/admin/users/{admin_user.id}", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_a_super_admin_cannot_be_deleted(
    client, admin_headers, created_user, db_session
):
    target_id = created_user.id
    with _super_admins(created_user.email):
        resp = await client.delete(
            f"/v1/admin/users/{target_id}", headers=admin_headers
        )
        assert_envelope_error(resp, expected_status=403)

    db_session.expire_all()
    refreshed = await db_session.get(User, target_id)
    assert refreshed is not None
    assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_deleting_404s_for_an_unknown_user(client, admin_headers):
    resp = await client.delete(f"/v1/admin/users/{uuid.uuid4()}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


# ── The audit trail ──


@pytest.mark.asyncio
async def test_a_ban_leaves_an_audit_row_naming_the_operator_and_the_reason(
    client, admin_headers, admin_user, db_session, second_user
):
    """Ban is the highest-blast-radius action an ordinary admin can take. The
    audit row is the only record of WHO did it — without it, "my account was
    banned and nobody knows why" has no answer."""
    actor_id, target_id = admin_user.id, second_user.id
    await client.post(
        f"/v1/admin/users/{target_id}/ban",
        json={"reason": "listing counterfeits"},
        headers=admin_headers,
    )

    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "user.ban")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == actor_id
    assert rows[0].target_id == str(target_id)
    assert rows[0].payload == {"reason": "listing counterfeits"}
