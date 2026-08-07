"""Legal document schemas — the contract for `/v1/public/legal` + `/v1/admin/legal`.

A *legal corpus* is the checked-in ``legal_documents.json`` (Terms, Privacy,
Cookies, Acceptable Use, Subprocessors, Market Data Disclosure) plus the live
operator edits layered over it. Section bodies are GitHub-flavoured Markdown;
``{{placeholder}}`` tokens resolve against the shared ``entity`` block so the
company name, jurisdiction, and contact addresses are edited in exactly one
place and propagate through every document.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

#: Slugs and section ids are URL fragments and anchor targets — keep them tame.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Entity keys become `{{token}}` placeholders, so they must be identifiers.
_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

Slug = Annotated[str, Field(min_length=1, max_length=48)]


class LegalSection(BaseModel):
    """One numbered clause. ``body`` is Markdown, rendered by the clients."""

    id: Slug
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=200_000)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"section id must be kebab-case: {v!r}")
        return v


class LegalDocument(BaseModel):
    """A whole document — the unit an operator edits and a client renders."""

    slug: Slug
    title: str = Field(min_length=1, max_length=120)
    lead: str = Field(default="", max_length=600)
    #: ISO date the document takes effect / was last revised.
    effective: str = Field(min_length=4, max_length=32)
    updated: str = Field(min_length=4, max_length=32)
    #: Plain-English "what this means" bullets shown above the legal text.
    summary: list[str] = Field(default_factory=list, max_length=12)
    sections: list[LegalSection] = Field(default_factory=list, max_length=80)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(f"document slug must be kebab-case: {v!r}")
        return v

    @field_validator("sections")
    @classmethod
    def _unique_ids(cls, v: list[LegalSection]) -> list[LegalSection]:
        ids = [s.id for s in v]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate section ids in document")
        return v


class LegalCorpus(BaseModel):
    """The checked-in file: shared entity block + every document."""

    version: int = 1
    entity: dict[str, str] = Field(default_factory=dict)
    documents: list[LegalDocument] = Field(default_factory=list)

    @field_validator("entity")
    @classmethod
    def _keys(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _KEY_RE.match(key):
                raise ValueError(f"entity key must be an identifier: {key!r}")
        return v


class LegalOverrides(BaseModel):
    """Operator edits layered over the file corpus at serve time.

    Legal copy is edited wholesale rather than field-by-field, so an override
    holds the **entire** replacement document keyed by slug. That makes "revert
    to the checked-in version" a single key deletion, and makes it impossible
    for a partial patch to leave a clause in a half-edited state.
    """

    #: Patches merged over the file entity (company name, jurisdiction, emails).
    entity: dict[str, str] = Field(default_factory=dict)
    #: slug -> full replacement (or an operator-authored document).
    documents: dict[str, LegalDocument] = Field(default_factory=dict)
    #: File documents an operator retired. Tombstoned, never erased, so the
    #: portal can always restore them.
    removed: list[str] = Field(default_factory=list, max_length=40)
    updated_at: datetime | None = None
    updated_by: str | None = Field(default=None, max_length=320)

    @field_validator("entity")
    @classmethod
    def _keys(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _KEY_RE.match(key):
                raise ValueError(f"entity key must be an identifier: {key!r}")
        return v


# ── Public read models ────────────────────────────────────────────────────


class LegalDocumentRead(LegalDocument):
    """A published document with every ``{{placeholder}}`` already resolved."""

    #: The whole document as one Markdown string (title, summary, then every
    #: section) — for clients that would rather render one blob than a list.
    markdown: str = ""


class LegalIndexEntry(BaseModel):
    slug: str
    title: str
    lead: str
    effective: str
    updated: str


class LegalIndex(BaseModel):
    documents: list[LegalIndexEntry]
    updated: str


# ── Admin models ──────────────────────────────────────────────────────────


class AdminLegalDocument(LegalDocument):
    """A merged document annotated for the portal (raw, NOT interpolated)."""

    #: "file" = checked-in; "custom" = operator-authored.
    origin: str = "file"
    #: A file document with a live operator override on it.
    edited: bool = False
    #: A retired file document — listed (restorable) but never served.
    removed: bool = False


class AdminLegalView(BaseModel):
    """Everything `/admin/legal` renders in one call."""

    #: Effective entity (file merged with overrides) — what placeholders use.
    entity: dict[str, str]
    #: The checked-in entity, so the portal can show + restore defaults.
    fileEntity: dict[str, str]
    documents: list[AdminLegalDocument]
    #: True when any override is live (entity patch, edit, or tombstone).
    dirty: bool = False
    updatedAt: datetime | None = None
    updatedBy: str | None = None


class LegalEntityUpdate(BaseModel):
    """Replace the operator entity patch wholesale."""

    entity: dict[str, str] = Field(default_factory=dict)

    @field_validator("entity")
    @classmethod
    def _keys(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _KEY_RE.match(key):
                raise ValueError(f"entity key must be an identifier: {key!r}")
        return v


__all__ = [
    "AdminLegalDocument",
    "AdminLegalView",
    "LegalCorpus",
    "LegalDocument",
    "LegalDocumentRead",
    "LegalEntityUpdate",
    "LegalIndex",
    "LegalIndexEntry",
    "LegalOverrides",
    "LegalSection",
]
