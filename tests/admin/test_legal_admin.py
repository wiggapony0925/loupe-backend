"""Router tests for `/v1/admin/legal` — counsel's live control over the corpus.

The published corpus is a contract with every user, and this portal edits it
without a deploy. Two things therefore have to be nailed down: only an admin
may touch it, and every mutation must be *reversible* — an edited or retired
document has to be restorable to its checked-in text, because "restore the
old Terms" is a request that arrives on the worst day of the quarter.

The merge/render rules themselves are unit-tested in tests/public/test_legal.py;
these cover the HTTP surface: the admin gate, the 404s, and the round trip
from a portal edit to what a reader is served.
"""

from __future__ import annotations

import pytest

from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the portal's caller.

    A fresh user rather than promoting ``created_user``, so the same run can
    assert an ordinary account is refused.
    """
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


def _document(
    slug: str = "cookies",
    *,
    title: str = "Cookie Policy",
    body: str = "Rewritten by counsel.",
) -> dict:
    """A minimal but valid `LegalDocument` body."""
    return {
        "slug": slug,
        "title": title,
        "lead": "",
        "effective": "2026-09-01",
        "updated": "2026-09-01",
        "summary": [],
        "sections": [{"id": "only", "heading": "1. Only", "body": body}],
    }


#: Every mutating route plus the two reads, as (method, path, json body).
_ROUTES = [
    ("GET", "/v1/admin/legal/unresolved", None),
    ("GET", "/v1/admin/legal/preview/terms", None),
    ("PUT", "/v1/admin/legal/entity", {"entity": {"legalName": "Someone Else LLC"}}),
    ("PUT", "/v1/admin/legal/cookies", _document()),
    ("POST", "/v1/admin/legal/cookies/reset", None),
    ("POST", "/v1/admin/legal/reset", None),
    ("DELETE", "/v1/admin/legal/cookies", None),
]


# ── RULE: only an admin edits the law ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
async def test_legal_routes_reject_anonymous_callers(client, method, path, body):
    resp = await client.request(method, path, json=body)
    assert_envelope_error(resp, expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
async def test_legal_routes_reject_an_ordinary_signed_in_user(
    client, auth_headers, method, path, body
):
    """Signed in is not the same as staff: a normal account cannot rewrite the
    arbitration clause, and must not even be able to read the portal view."""
    resp = await client.request(method, path, headers=auth_headers, json=body)
    assert_envelope_error(resp, expected_status=403)


# ── Preview ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_renders_a_document_the_way_a_reader_sees_it(
    client, admin_headers
):
    """The point of preview is to catch a broken edit *before* publishing, so
    it has to resolve placeholders exactly like the public route does."""
    data = assert_envelope_ok(
        await client.get("/v1/admin/legal/preview/terms", headers=admin_headers)
    )
    assert data["slug"] == "terms"
    assert data["markdown"]
    assert "{{" not in data["markdown"], "unresolved placeholder in the preview"
    assert "JFM Capital Group LLC" in data["markdown"]


@pytest.mark.asyncio
async def test_preview_404s_on_an_unknown_slug(client, admin_headers):
    resp = await client.get(
        "/v1/admin/legal/preview/not-a-document", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_preview_shows_a_retired_document_even_though_readers_cannot(
    client, admin_headers
):
    """Documents the corpus tombstones are still previewable — that is how an
    operator checks what they are about to restore. Documents this behaviour
    rather than endorsing it: preview reads the *merged* list, not the
    published one."""
    assert_envelope_ok(
        await client.delete("/v1/admin/legal/cookies", headers=admin_headers)
    )
    assert_envelope_error(
        await client.get("/v1/public/legal/cookies"), expected_status=404
    )
    preview = assert_envelope_ok(
        await client.get("/v1/admin/legal/preview/cookies", headers=admin_headers)
    )
    assert preview["slug"] == "cookies"


# ── Unresolved placeholders ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unresolved_is_empty_for_the_checked_in_corpus(client, admin_headers):
    data = assert_envelope_ok(
        await client.get("/v1/admin/legal/unresolved", headers=admin_headers)
    )
    assert data == []


@pytest.mark.asyncio
async def test_unresolved_names_a_placeholder_with_no_entity_key(client, admin_headers):
    """The portal's warning banner: an operator who types `{{legalNam}}` finds
    out here, not from a reader seeing it inside a contract."""
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/cookies",
            headers=admin_headers,
            json=_document(body="Operated by {{legalNam}} in {{governingState}}."),
        )
    )
    data = assert_envelope_ok(
        await client.get("/v1/admin/legal/unresolved", headers=admin_headers)
    )
    assert data == ["legalNam"], "only the token missing from the entity block"


# ── Entity block ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_editing_the_entity_propagates_to_every_document(client, admin_headers):
    """The whole reason the entity block exists: rename the company once and
    ~20,000 words of published copy follow, with no deploy."""
    view = assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/entity",
            headers=admin_headers,
            json={"entity": {"legalName": "Someone Else LLC"}},
        )
    )
    assert view["entity"]["legalName"] == "Someone Else LLC"
    assert view["fileEntity"]["legalName"] == "JFM Capital Group LLC", (
        "the checked-in value must stay visible so the portal can restore it"
    )
    assert view["dirty"] is True

    served = assert_envelope_ok(await client.get("/v1/public/legal/terms"))
    assert "Someone Else LLC" in served["markdown"]


@pytest.mark.asyncio
async def test_entity_patch_only_records_genuine_differences(client, admin_headers):
    """Saving the form unchanged must not leave the corpus 'dirty' — otherwise
    the portal shows a permanent override banner nobody can clear."""
    view = assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/entity",
            headers=admin_headers,
            json={"entity": {"legalName": "JFM Capital Group LLC"}},
        )
    )
    assert view["dirty"] is False


@pytest.mark.asyncio
async def test_entity_keys_must_be_placeholder_identifiers(client, admin_headers):
    """Entity keys become `{{token}}`s, so a key with a space or a dash could
    never be referenced — reject it at the door instead of storing dead data."""
    resp = await client.put(
        "/v1/admin/legal/entity",
        headers=admin_headers,
        json={"entity": {"legal name": "Someone Else LLC"}},
    )
    assert_envelope_error(resp, expected_status=422)


@pytest.mark.asyncio
async def test_updating_the_entity_is_audit_logged(client, admin_headers, db_session):
    """ "Who changed the arbitration clause, and when" is a question you get
    asked once, at the worst possible moment."""
    from sqlalchemy import select

    from app.models.audit import AuditLog

    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/entity",
            headers=admin_headers,
            json={"entity": {"legalName": "Someone Else LLC"}},
        )
    )
    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "legal.entity.update")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].target_id == "entity"
    assert rows[0].payload == {"keys": ["legalName"]}


# ── Publishing a document ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publishing_an_edit_reaches_readers_immediately(client, admin_headers):
    view = assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/cookies", headers=admin_headers, json=_document()
        )
    )
    cookies = next(d for d in view["documents"] if d["slug"] == "cookies")
    assert cookies["edited"] is True
    assert cookies["origin"] == "file", "an edited file document is still a file one"
    assert view["updatedBy"], "the acting admin is recorded on the override document"

    served = assert_envelope_ok(await client.get("/v1/public/legal/cookies"))
    assert served["sections"][0]["body"] == "Rewritten by counsel."


@pytest.mark.asyncio
async def test_publishing_a_new_slug_creates_a_custom_document(client, admin_headers):
    """Counsel can add a document the codebase has never heard of (a DPA, a
    regional addendum) without a migration."""
    view = assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/dpa",
            headers=admin_headers,
            json=_document("dpa", title="Data Processing Addendum"),
        )
    )
    dpa = next(d for d in view["documents"] if d["slug"] == "dpa")
    assert dpa["origin"] == "custom"

    served = assert_envelope_ok(await client.get("/v1/public/legal/dpa"))
    assert served["title"] == "Data Processing Addendum"


@pytest.mark.asyncio
async def test_publishing_rejects_a_body_whose_slug_disagrees_with_the_path(
    client, admin_headers
):
    """A mismatch means the operator is about to overwrite a document they are
    not looking at — refuse rather than guess which one they meant."""
    resp = await client.put(
        "/v1/admin/legal/cookies", headers=admin_headers, json=_document("terms")
    )
    assert_envelope_error(resp, expected_status=400)


@pytest.mark.asyncio
async def test_publishing_rejects_duplicate_section_ids(client, admin_headers):
    """Section ids are anchor targets; duplicates make deep links ambiguous."""
    payload = _document()
    payload["sections"] = [
        {"id": "only", "heading": "1. One", "body": "A."},
        {"id": "only", "heading": "2. Two", "body": "B."},
    ]
    resp = await client.put(
        "/v1/admin/legal/cookies", headers=admin_headers, json=payload
    )
    assert_envelope_error(resp, expected_status=422)


# ── Reset / retire ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resetting_a_document_restores_its_checked_in_text(client, admin_headers):
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/cookies", headers=admin_headers, json=_document()
        )
    )
    view = assert_envelope_ok(
        await client.post("/v1/admin/legal/cookies/reset", headers=admin_headers)
    )
    cookies = next(d for d in view["documents"] if d["slug"] == "cookies")
    assert cookies["edited"] is False
    assert view["dirty"] is False

    served = assert_envelope_ok(await client.get("/v1/public/legal/cookies"))
    assert served["sections"][0]["body"] != "Rewritten by counsel."


@pytest.mark.asyncio
async def test_resetting_a_custom_document_404s(client, admin_headers):
    """There is no checked-in text to go back to, so 'reset' is meaningless —
    the operator wants DELETE instead, and should be told so."""
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/dpa", headers=admin_headers, json=_document("dpa")
        )
    )
    resp = await client.post("/v1/admin/legal/dpa/reset", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_retiring_a_file_document_tombstones_it_restorably(client, admin_headers):
    """Retiring a published policy is a legal act; it must never be a one-way
    door. The portal keeps listing it so it can be restored in one click."""
    view = assert_envelope_ok(
        await client.delete("/v1/admin/legal/cookies", headers=admin_headers)
    )
    cookies = next(d for d in view["documents"] if d["slug"] == "cookies")
    assert cookies["removed"] is True

    assert_envelope_error(
        await client.get("/v1/public/legal/cookies"), expected_status=404
    )
    index = assert_envelope_ok(await client.get("/v1/public/legal"))
    assert "cookies" not in {d["slug"] for d in index["documents"]}

    restored = assert_envelope_ok(
        await client.post("/v1/admin/legal/cookies/reset", headers=admin_headers)
    )
    assert (
        next(d for d in restored["documents"] if d["slug"] == "cookies")["removed"]
        is False
    )
    assert_envelope_ok(await client.get("/v1/public/legal/cookies"))


@pytest.mark.asyncio
async def test_retiring_a_custom_document_drops_it_entirely(client, admin_headers):
    """Nothing to restore it from, so a custom document is really deleted —
    and deleting it twice is a 404, not a silent success."""
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/dpa", headers=admin_headers, json=_document("dpa")
        )
    )
    view = assert_envelope_ok(
        await client.delete("/v1/admin/legal/dpa", headers=admin_headers)
    )
    assert "dpa" not in {d["slug"] for d in view["documents"]}
    assert_envelope_error(
        await client.delete("/v1/admin/legal/dpa", headers=admin_headers),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_retiring_an_unknown_document_404s(client, admin_headers):
    resp = await client.delete("/v1/admin/legal/not-a-document", headers=admin_headers)
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_reset_all_discards_every_operator_edit(client, admin_headers):
    """The panic button: whatever counsel did to the corpus, one call puts the
    site back on the text that shipped with the build."""
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/entity",
            headers=admin_headers,
            json={"entity": {"legalName": "Someone Else LLC"}},
        )
    )
    assert_envelope_ok(
        await client.put(
            "/v1/admin/legal/cookies", headers=admin_headers, json=_document()
        )
    )
    assert_envelope_ok(
        await client.delete("/v1/admin/legal/terms", headers=admin_headers)
    )

    view = assert_envelope_ok(
        await client.post("/v1/admin/legal/reset", headers=admin_headers)
    )
    assert view["dirty"] is False
    assert view["entity"] == view["fileEntity"]
    assert all(not d["edited"] and not d["removed"] for d in view["documents"])

    index = assert_envelope_ok(await client.get("/v1/public/legal"))
    assert {"terms", "cookies"} <= {d["slug"] for d in index["documents"]}
