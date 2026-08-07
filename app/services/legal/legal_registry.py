"""Operator-controlled legal document registry.

The published corpus — Terms of Service, Privacy Policy, Cookie Policy,
Acceptable Use Policy, Subprocessors, and the Market Data & Valuation
Disclosure — lives in a checked-in JSON file (``legal_documents.json``),
validated through :class:`LegalCorpus` at import so a malformed file fails CI
or startup, never a reader.

Counsel gets *live* control over it from the developer portal ("Law") WITHOUT
a deploy or a DB migration: their edits are one :class:`LegalOverrides` JSON
document in ``kv_cache`` (Postgres — shared by every instance, survives
restarts) under a well-known key, merged over the file at serve time. Retired
*file* documents are tombstoned rather than erased so the portal can always
restore them; retired *custom* documents are simply dropped.

Section bodies are GitHub-flavoured Markdown. ``{{placeholder}}`` tokens are
resolved against the merged ``entity`` block on the way out, so the company
name, jurisdiction, and contact addresses are edited in one place and
propagate through all ~20,000 words. Unknown tokens are left verbatim rather
than blanked — a visible ``{{typo}}`` is a bug report; a silently empty
company name in a contract is a liability.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.platform.cache_l2 import kv_get, kv_set
from app.schemas.legal import (
    AdminLegalDocument,
    LegalCorpus,
    LegalDocument,
    LegalDocumentRead,
    LegalIndexEntry,
    LegalOverrides,
    LegalSection,
)
from app.utils.logger import get_logger

logger = get_logger("services.legal_registry")

_CORPUS_PATH = Path(__file__).with_name("legal_documents.json")

#: Where the operator override document lives in ``kv_cache``.
OVERRIDES_KEY = "legal:operator_overrides:v1"
#: ``kv_cache`` rows need an expiry; these are settings, not cache — use an
#: effectively-forever TTL, refreshed on every save.
_OVERRIDES_TTL = 10 * 365 * 24 * 60 * 60

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def _load_file_corpus() -> LegalCorpus:
    """Parse + validate the checked-in corpus. Raises at import on a bad file
    so a broken deploy is caught by CI/startup, never by a reader."""
    doc = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
    corpus = LegalCorpus.model_validate(doc)
    slugs = [d.slug for d in corpus.documents]
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"legal_documents.json has duplicate slugs: {slugs}")
    return corpus


#: The checked-in corpus, loaded once at import.
FILE_CORPUS: LegalCorpus = _load_file_corpus()
_FILE_BY_SLUG: dict[str, LegalDocument] = {d.slug: d for d in FILE_CORPUS.documents}


# ──────────────────────────────────────────────────────────────────────────
# Override document
# ──────────────────────────────────────────────────────────────────────────


async def get_overrides() -> LegalOverrides:
    """The live operator override document (defaults when unset/corrupt)."""
    raw = await kv_get(OVERRIDES_KEY)
    if raw:
        try:
            return LegalOverrides.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning("legal overrides document is invalid, ignoring: %s", exc)
    return LegalOverrides()


async def save_overrides(overrides: LegalOverrides, *, actor: str | None) -> None:
    overrides.updated_at = datetime.now(UTC)
    overrides.updated_by = actor
    await kv_set(OVERRIDES_KEY, overrides.model_dump_json(), _OVERRIDES_TTL)


# ──────────────────────────────────────────────────────────────────────────
# Merge — file + overrides → what the portal shows / the site serves
# ──────────────────────────────────────────────────────────────────────────


def merged_entity(overrides: LegalOverrides) -> dict[str, str]:
    """File entity with the operator patch layered on top."""
    return {**FILE_CORPUS.entity, **overrides.entity}


def merged_documents(overrides: LegalOverrides) -> list[AdminLegalDocument]:
    """Every document the portal should list, annotated with its origin.

    File order is preserved (it is the order counsel intends them read in);
    operator-authored documents are appended. Tombstoned file documents are
    included and flagged ``removed`` so the portal can restore them — callers
    that serve readers must filter them out (:func:`published_documents`).
    """
    out: list[AdminLegalDocument] = []
    for file_doc in FILE_CORPUS.documents:
        override = overrides.documents.get(file_doc.slug)
        source = override or file_doc
        out.append(
            AdminLegalDocument(
                **source.model_dump(),
                origin="file",
                edited=override is not None,
                removed=file_doc.slug in overrides.removed,
            )
        )
    for slug, custom in overrides.documents.items():
        if slug in _FILE_BY_SLUG:
            continue
        out.append(
            AdminLegalDocument(**custom.model_dump(), origin="custom", edited=True)
        )
    return out


def published_documents(overrides: LegalOverrides) -> list[AdminLegalDocument]:
    """What readers actually get — merged, minus tombstones."""
    return [d for d in merged_documents(overrides) if not d.removed]


def is_dirty(overrides: LegalOverrides) -> bool:
    return bool(overrides.entity or overrides.documents or overrides.removed)


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────


def interpolate(text: str, entity: dict[str, str]) -> str:
    """Resolve ``{{token}}`` against *entity*, leaving unknown tokens intact."""
    return _PLACEHOLDER.sub(lambda m: entity.get(m.group(1), m.group(0)), text)


def unresolved_tokens(overrides: LegalOverrides) -> list[str]:
    """Placeholders used somewhere but missing from the entity block.

    Surfaced in the portal so an operator who renames an entity key finds out
    from a warning banner rather than from a reader seeing ``{{legalName}}``
    in a contract.
    """
    entity = merged_entity(overrides)
    found: set[str] = set()
    for doc in published_documents(overrides):
        blobs = [doc.title, doc.lead, *doc.summary]
        blobs += [s.heading for s in doc.sections]
        blobs += [s.body for s in doc.sections]
        for blob in blobs:
            found.update(_PLACEHOLDER.findall(blob))
    return sorted(t for t in found if t not in entity)


def _document_markdown(doc: LegalDocument) -> str:
    """The whole document as one Markdown string."""
    parts = [f"# {doc.title}"]
    if doc.lead:
        parts.append(f"_{doc.lead}_")
    parts.append(f"**Last updated:** {doc.updated}  \n**Effective:** {doc.effective}")
    if doc.summary:
        parts.append("## In short")
        parts.append("\n".join(f"- {line}" for line in doc.summary))
    for section in doc.sections:
        parts.append(f"## {section.heading}")
        parts.append(section.body)
    return "\n\n".join(parts)


def render(doc: LegalDocument, entity: dict[str, str]) -> LegalDocumentRead:
    """Resolve every placeholder and attach the assembled Markdown."""
    resolved = LegalDocument(
        slug=doc.slug,
        title=interpolate(doc.title, entity),
        lead=interpolate(doc.lead, entity),
        effective=doc.effective,
        updated=doc.updated,
        summary=[interpolate(line, entity) for line in doc.summary],
        sections=[
            LegalSection(
                id=s.id,
                heading=interpolate(s.heading, entity),
                body=interpolate(s.body, entity),
            )
            for s in doc.sections
        ],
    )
    return LegalDocumentRead(
        **resolved.model_dump(), markdown=_document_markdown(resolved)
    )


async def published(slug: str) -> LegalDocumentRead | None:
    """One fully-rendered document, or ``None`` if it is unknown/retired."""
    overrides = await get_overrides()
    entity = merged_entity(overrides)
    for doc in published_documents(overrides):
        if doc.slug == slug:
            return render(doc, entity)
    return None


async def index() -> tuple[list[LegalIndexEntry], str]:
    """Index entries plus the most recent ``updated`` date across the corpus."""
    overrides = await get_overrides()
    entity = merged_entity(overrides)
    docs = published_documents(overrides)
    entries = [
        LegalIndexEntry(
            slug=d.slug,
            title=interpolate(d.title, entity),
            lead=interpolate(d.lead, entity),
            effective=d.effective,
            updated=d.updated,
        )
        for d in docs
    ]
    latest = max((d.updated for d in docs), default="")
    return entries, latest


# ──────────────────────────────────────────────────────────────────────────
# Mutations (portal)
# ──────────────────────────────────────────────────────────────────────────


class UnknownDocumentError(LookupError):
    """Raised when an operator targets a slug that does not exist."""


async def put_document(doc: LegalDocument, *, actor: str | None) -> LegalOverrides:
    """Upsert a full document override (creates a custom doc for a new slug)."""
    overrides = await get_overrides()
    overrides.documents[doc.slug] = doc
    # Saving a retired document is the operator un-retiring it.
    if doc.slug in overrides.removed:
        overrides.removed.remove(doc.slug)
    await save_overrides(overrides, actor=actor)
    return overrides


async def remove_document(slug: str, *, actor: str | None) -> LegalOverrides:
    """Retire a document: tombstone a file one, drop a custom one."""
    overrides = await get_overrides()
    if slug in _FILE_BY_SLUG:
        if slug not in overrides.removed:
            overrides.removed.append(slug)
    elif slug in overrides.documents:
        del overrides.documents[slug]
    else:
        raise UnknownDocumentError(slug)
    await save_overrides(overrides, actor=actor)
    return overrides


async def reset_document(slug: str, *, actor: str | None) -> LegalOverrides:
    """Restore a file document to its checked-in text (and un-retire it)."""
    if slug not in _FILE_BY_SLUG:
        raise UnknownDocumentError(slug)
    overrides = await get_overrides()
    overrides.documents.pop(slug, None)
    if slug in overrides.removed:
        overrides.removed.remove(slug)
    await save_overrides(overrides, actor=actor)
    return overrides


async def put_entity(entity: dict[str, str], *, actor: str | None) -> LegalOverrides:
    """Replace the operator entity patch (keys equal to the file value are
    dropped, so the patch only ever records genuine differences)."""
    overrides = await get_overrides()
    overrides.entity = {
        k: v for k, v in entity.items() if FILE_CORPUS.entity.get(k) != v
    }
    await save_overrides(overrides, actor=actor)
    return overrides


async def reset_all(*, actor: str | None) -> LegalOverrides:
    """Discard every operator edit — back to the checked-in corpus."""
    overrides = LegalOverrides()
    await save_overrides(overrides, actor=actor)
    return overrides


__all__ = [
    "FILE_CORPUS",
    "OVERRIDES_KEY",
    "UnknownDocumentError",
    "get_overrides",
    "index",
    "interpolate",
    "is_dirty",
    "merged_documents",
    "merged_entity",
    "published",
    "published_documents",
    "put_document",
    "put_entity",
    "remove_document",
    "render",
    "reset_all",
    "reset_document",
    "save_overrides",
    "unresolved_tokens",
]
