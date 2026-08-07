"""Legal corpus contract + `/v1/public/legal/*`.

These are guard-rail tests, not coverage theatre. The corpus is a published
contract with every user; a typo that blanks the company name or an operator
edit that silently drops the arbitration clause is the kind of bug you find out
about from a lawyer, so it gets caught here instead.
"""

from __future__ import annotations

import pytest

from app.schemas.legal import LegalDocument, LegalOverrides, LegalSection
from app.services.legal import legal_registry

#: Documents we must always publish. Retiring one is a deliberate legal act;
#: it should break this test and make somebody think about it.
REQUIRED_SLUGS = {
    "terms",
    "privacy",
    "cookies",
    "acceptable-use",
    "subprocessors",
    "disclosure",
}


def test_file_corpus_loads_and_is_complete():
    slugs = {d.slug for d in legal_registry.FILE_CORPUS.documents}
    assert slugs >= REQUIRED_SLUGS
    assert len(slugs) == len(legal_registry.FILE_CORPUS.documents), "duplicate slug"
    for doc in legal_registry.FILE_CORPUS.documents:
        assert doc.sections, f"{doc.slug} has no sections"
        assert doc.title and doc.effective and doc.updated


def test_every_placeholder_resolves():
    """A `{{legalName}}` leaking into a published contract is a real defect."""
    assert legal_registry.unresolved_tokens(LegalOverrides()) == []


def test_terms_retains_its_load_bearing_clauses():
    """The clauses that actually allocate risk must not quietly disappear."""
    overrides = LegalOverrides()
    entity = legal_registry.merged_entity(overrides)
    terms = next(
        d for d in legal_registry.published_documents(overrides) if d.slug == "terms"
    )
    body = legal_registry.render(terms, entity).markdown.lower()
    for phrase in (
        "binding arbitration",
        "class-action",
        "limitation of liability",
        "disclaimer of warranties",
        "governing law",
        "third-party beneficiar",  # Apple's required App Store clause
        "investment advice",
    ):
        assert phrase in body, f"Terms lost its {phrase!r} clause"


def test_interpolation_leaves_unknown_tokens_visible():
    """Better a visible `{{typo}}` than a silently blank company name."""
    out = legal_registry.interpolate("{{known}} and {{missing}}", {"known": "Loupe"})
    assert out == "Loupe and {{missing}}"


def test_overrides_layer_over_the_file(monkeypatch):
    edited = LegalDocument(
        slug="cookies",
        title="Cookie Policy",
        lead="",
        effective="2026-09-01",
        updated="2026-09-01",
        summary=[],
        sections=[LegalSection(id="one", heading="1. Only", body="Rewritten.")],
    )
    overrides = LegalOverrides(documents={"cookies": edited})

    merged = {d.slug: d for d in legal_registry.merged_documents(overrides)}
    assert merged["cookies"].edited is True
    assert merged["cookies"].origin == "file"
    assert merged["cookies"].sections[0].body == "Rewritten."
    # Untouched documents still come from the file.
    assert merged["terms"].edited is False


def test_tombstoned_documents_are_listed_but_not_published():
    overrides = LegalOverrides(removed=["cookies"])
    listed = {d.slug: d for d in legal_registry.merged_documents(overrides)}
    published = {d.slug for d in legal_registry.published_documents(overrides)}

    assert listed["cookies"].removed is True, "portal must still offer a restore"
    assert "cookies" not in published


def test_custom_documents_are_appended():
    custom = LegalDocument(
        slug="dpa",
        title="Data Processing Addendum",
        lead="",
        effective="2026-08-07",
        updated="2026-08-07",
        sections=[LegalSection(id="scope", heading="1. Scope", body="Applies.")],
    )
    overrides = LegalOverrides(documents={"dpa": custom})
    merged = {d.slug: d for d in legal_registry.merged_documents(overrides)}
    assert merged["dpa"].origin == "custom"


def test_entity_patch_propagates_through_every_document():
    overrides = LegalOverrides(entity={"legalName": "Someone Else LLC"})
    entity = legal_registry.merged_entity(overrides)
    terms = next(
        d for d in legal_registry.published_documents(overrides) if d.slug == "terms"
    )
    assert "Someone Else LLC" in legal_registry.render(terms, entity).markdown


@pytest.mark.asyncio
async def test_public_index_and_document_are_open(client):
    """No auth: a Terms of Service you must sign in to read is not one."""
    resp = await client.get("/v1/public/legal")
    assert resp.status_code == 200
    slugs = {d["slug"] for d in resp.json()["data"]["documents"]}
    assert slugs >= REQUIRED_SLUGS

    resp = await client.get("/v1/public/legal/privacy")
    assert resp.status_code == 200
    doc = resp.json()["data"]
    assert doc["title"] == "Privacy Policy"
    assert doc["markdown"]
    assert "{{" not in doc["markdown"], "unresolved placeholder served to a reader"
    assert "Cache-Control" in resp.headers


@pytest.mark.asyncio
async def test_public_document_404s_on_unknown_slug(client):
    resp = await client.get("/v1/public/legal/not-a-document")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_legal_requires_admin(client):
    resp = await client.get("/v1/admin/legal")
    assert resp.status_code in (401, 403)
