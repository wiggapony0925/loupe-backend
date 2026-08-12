"""Router tests for the admin site config (`/v1/admin/config`).

One singleton row decides two live things: the shape of Loupe Pro (what the
free tier may hold, and which features are actually gated) and the global
announcement banner. Both take effect for every user immediately, with no
deploy, so the rules worth pinning are: only an admin may touch it, a partial
update must not silently reset the fields it didn't mention, and "unlimited"
has to be expressible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user

DEFAULT_PLAN = {
    "free_card_limit": 50,
    "free_statement_limit": 1,
    "gate_unlimited_cards": True,
    "gate_scanner_import": True,
    "gate_full_history": True,
    "gate_unlimited_alerts": True,
    "gate_statements": True,
}


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the developer portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


async def _patch_plan(client, headers, **fields) -> dict:
    return assert_envelope_ok(
        await client.patch("/v1/admin/config/plan", json=fields, headers=headers)
    )


async def _patch_announcement(client, headers, **fields) -> dict:
    return assert_envelope_ok(
        await client.patch(
            "/v1/admin/config/announcement", json=fields, headers=headers
        )
    )


# ── authorization ──


@pytest.mark.asyncio
async def test_the_config_is_not_readable_anonymously(client):
    """It exposes the commercial shape of the product (limits, what's gated)
    before any of it is public, so an unauthenticated read is refused."""
    assert_envelope_error(await client.get("/v1/admin/config"), expected_status=401)


@pytest.mark.asyncio
async def test_an_ordinary_user_can_neither_read_nor_change_the_config(
    client, auth_headers
):
    """A signed-in user changing this would re-price the product for everyone,
    so the whole surface — read included — is admin-only."""
    responses = [
        await client.get("/v1/admin/config", headers=auth_headers),
        await client.patch(
            "/v1/admin/config/plan",
            json={"gate_statements": False},
            headers=auth_headers,
        ),
        await client.patch(
            "/v1/admin/config/announcement",
            json={"enabled": True},
            headers=auth_headers,
        ),
    ]
    for resp in responses:
        assert_envelope_error(resp, expected_status=403)


# ── reading ──


@pytest.mark.asyncio
async def test_the_first_read_creates_the_config_with_its_defaults(
    client, admin_headers
):
    """Nothing seeds this row, so the portal's first-ever visit has to produce
    the shipped defaults rather than a 404 or an empty plan."""
    body = assert_envelope_ok(
        await client.get("/v1/admin/config", headers=admin_headers)
    )

    assert body["plan"] == DEFAULT_PLAN
    assert body["announcement"] == {
        "enabled": False,
        "message": "",
        "tone": "info",
        "cta_label": None,
        "cta_href": None,
    }
    assert body["updated_at"] is not None


@pytest.mark.asyncio
async def test_reading_the_config_twice_does_not_create_a_second_row(
    client, admin_headers, db_session
):
    """The config is a singleton — a second row would make which one wins a
    matter of query order, and edits would appear to be lost at random."""
    from app.models.site_config import SiteConfig

    await client.get("/v1/admin/config", headers=admin_headers)
    await client.get("/v1/admin/config", headers=admin_headers)

    rows = (await db_session.execute(select(SiteConfig.id))).all()
    assert len(rows) == 1


# ── plan ──


@pytest.mark.asyncio
async def test_ungating_one_feature_leaves_the_rest_of_the_plan_alone(
    client, admin_headers
):
    """The portal sends only the toggle that moved. If unsent fields fell back
    to their defaults, freeing one feature would quietly re-gate the others."""
    await _patch_plan(client, admin_headers, free_card_limit=250)

    body = await _patch_plan(client, admin_headers, gate_statements=False)

    assert body["plan"]["gate_statements"] is False
    assert body["plan"]["free_card_limit"] == 250  # survived the second write
    assert body["plan"]["gate_scanner_import"] is True


@pytest.mark.asyncio
async def test_clearing_the_card_limit_makes_the_free_tier_unlimited(
    client, admin_headers
):
    """`null` is the plan's word for "no cap". It has to be reachable from the
    portal, otherwise the free tier can only ever be tightened, never opened."""
    body = await _patch_plan(client, admin_headers, clear_card_limit=True)
    assert body["plan"]["free_card_limit"] is None
    # The statement limit is a separate switch and stays where it was.
    assert body["plan"]["free_statement_limit"] == 1

    body = await _patch_plan(client, admin_headers, clear_statement_limit=True)
    assert body["plan"]["free_statement_limit"] is None


@pytest.mark.asyncio
async def test_clearing_a_limit_wins_over_a_number_sent_in_the_same_request(
    client, admin_headers
):
    """Both arrive in one body; the clear flag is applied last, so a confused
    client asking for "10, and also unlimited" ends up unlimited."""
    body = await _patch_plan(
        client, admin_headers, free_card_limit=10, clear_card_limit=True
    )
    assert body["plan"]["free_card_limit"] is None


@pytest.mark.asyncio
async def test_a_negative_free_limit_is_rejected(client, admin_headers):
    """A negative cap has no meaning downstream — it would read as "unlimited"
    or lock every free user out, depending on the comparison."""
    resp = await client.patch(
        "/v1/admin/config/plan", json={"free_card_limit": -1}, headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_a_free_limit_above_the_ceiling_is_rejected(client, admin_headers):
    """A six-figure cap is indistinguishable from unlimited but keeps the
    counting query running, so the schema caps it at 100k."""
    resp = await client.patch(
        "/v1/admin/config/plan",
        json={"free_statement_limit": 100_001},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


# ── announcement ──


@pytest.mark.asyncio
async def test_publishing_an_announcement_leaves_the_plan_shape_untouched(
    client, admin_headers
):
    """Both live on the same row. Writing the banner must not disturb pricing —
    they are edited by different people at different times."""
    body = await _patch_announcement(
        client,
        admin_headers,
        enabled=True,
        message="Scheduled maintenance at 02:00 UTC.",
        tone="warning",
        cta_label="Status",
        cta_href="https://status.example.com",
    )

    assert body["announcement"] == {
        "enabled": True,
        "message": "Scheduled maintenance at 02:00 UTC.",
        "tone": "warning",
        "cta_label": "Status",
        "cta_href": "https://status.example.com",
    }
    assert body["plan"] == DEFAULT_PLAN


@pytest.mark.asyncio
async def test_switching_the_banner_off_keeps_the_message_for_next_time(
    client, admin_headers
):
    """Off is a separate field from the text, so an admin can retire a banner
    and bring the same wording back without retyping it."""
    await _patch_announcement(client, admin_headers, enabled=True, message="Hello")

    body = await _patch_announcement(client, admin_headers, enabled=False)
    assert body["announcement"]["enabled"] is False
    assert body["announcement"]["message"] == "Hello"


@pytest.mark.asyncio
async def test_an_unknown_banner_tone_is_rejected(client, admin_headers):
    """Tone drives the banner's colour on every client; an unrecognised value
    would render as an unstyled or invisible bar in production."""
    resp = await client.patch(
        "/v1/admin/config/announcement",
        json={"tone": "chartreuse"},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_a_message_longer_than_the_banner_column_is_rejected(
    client, admin_headers
):
    """The column is 500 chars — validating up front turns a would-be 500 at
    write time into a field-level error the portal can show."""
    resp = await client.patch(
        "/v1/admin/config/announcement",
        json={"message": "x" * 501},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


# ── null handling ──


@pytest.mark.asyncio
async def test_an_explicit_null_message_is_rejected(client, admin_headers):
    """The banner text is NOT NULL — there is no "no message" state, only an
    empty one — so a null is a client mistake and is refused up front rather
    than being carried into the write and failing at the database."""
    resp = await client.patch(
        "/v1/admin/config/announcement",
        json={"message": None},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_an_explicit_null_gate_is_rejected(client, admin_headers):
    """Every `gate_*` is NOT NULL: a feature is either gated or it isn't. Null
    is not "leave it" — omitting the field is — so it earns a 422."""
    resp = await client.patch(
        "/v1/admin/config/plan",
        json={"gate_statements": None},
        headers=admin_headers,
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_omitting_a_field_still_leaves_it_untouched(client, admin_headers):
    """Rejecting explicit nulls must not cost the partial update: a body that
    simply doesn't mention a field leaves that field exactly as it was."""
    await _patch_announcement(
        client, admin_headers, enabled=True, message="Hi", tone="warning"
    )

    body = await _patch_announcement(client, admin_headers, message="Bye")
    assert body["announcement"]["message"] == "Bye"
    assert body["announcement"]["enabled"] is True
    assert body["announcement"]["tone"] == "warning"

    body = await _patch_plan(client, admin_headers, gate_statements=False)
    assert body["plan"]["gate_statements"] is False
    assert body["plan"]["gate_scanner_import"] is True


@pytest.mark.asyncio
async def test_a_null_cta_clears_the_banner_link(client, admin_headers):
    """The CTA columns *are* nullable — null is how an admin removes the button
    from an otherwise unchanged banner — so those nulls stay legal."""
    await _patch_announcement(
        client,
        admin_headers,
        message="Maintenance",
        cta_label="Status",
        cta_href="https://status.example.com",
    )

    body = await _patch_announcement(
        client, admin_headers, cta_label=None, cta_href=None
    )
    assert body["announcement"]["cta_label"] is None
    assert body["announcement"]["cta_href"] is None
    assert body["announcement"]["message"] == "Maintenance"


@pytest.mark.asyncio
async def test_a_null_free_limit_is_accepted_as_unlimited(client, admin_headers):
    """The nullable columns take null happily, which means `clear_card_limit`
    is belt-and-braces rather than the only route to "unlimited"."""
    body = await _patch_plan(client, admin_headers, free_card_limit=None)
    assert body["plan"]["free_card_limit"] is None


# ── audit ──


@pytest.mark.asyncio
async def test_config_changes_are_attributed_to_the_admin_who_made_them(
    client, admin_headers, admin_user, db_session
):
    """Changing the plan changes what customers get for their money; the audit
    row is the only record of who moved it and to what."""
    await _patch_plan(client, admin_headers, free_card_limit=250)
    await _patch_announcement(client, admin_headers, enabled=True, message="Hi")

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_table == "site_config")
            )
        )
        .scalars()
        .all()
    )
    by_action = {r.action: r for r in rows}
    assert set(by_action) == {"config.plan", "config.announcement"}
    assert all(r.user_id == admin_user.id for r in rows)
    # Only the fields actually sent are recorded, so the log reads as a diff.
    assert by_action["config.plan"].payload == {"free_card_limit": 250}
    assert by_action["config.announcement"].payload == {
        "enabled": True,
        "message": "Hi",
    }
