"""Schemas for the careers + blog developer portal.

Covers the public surface (browse open jobs / published posts, submit and
track an application) and the admin surface (CRUD + applicant pipeline).
Status fields are validated against the enums in
:mod:`app.models.enums` but stored as their string values.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import (
    ApplicationStatusEnum,
    BlogStatusEnum,
    EmploymentTypeEnum,
    JobStatusEnum,
)

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def slugify(value: str) -> str:
    """Lowercase, hyphenated, URL-safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "untitled"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CTRL_RE.sub("", value).strip()
    return cleaned or None


# ── Jobs ────────────────────────────────────────────────────────────────


class JobPostingRead(BaseModel):
    """Full job posting (admin + public; status drives visibility)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    team: str
    location: str
    employment_type: EmploymentTypeEnum
    summary: str
    description: str
    status: JobStatusEnum
    created_at: datetime
    updated_at: datetime


class JobPostingCreate(BaseModel):
    """Admin: create a job posting. `slug` is derived from the title when omitted."""

    title: str = Field(..., min_length=2, max_length=160)
    team: str = Field(..., min_length=1, max_length=80)
    location: str = Field(..., min_length=1, max_length=120)
    employment_type: EmploymentTypeEnum = EmploymentTypeEnum.full_time
    summary: str = Field(..., min_length=1, max_length=400)
    description: str = Field("", max_length=20000)
    status: JobStatusEnum = JobStatusEnum.draft
    slug: str | None = Field(None, max_length=160)


class JobPostingUpdate(BaseModel):
    """Admin: partial update. Send only the fields that changed."""

    title: str | None = Field(None, min_length=2, max_length=160)
    team: str | None = Field(None, min_length=1, max_length=80)
    location: str | None = Field(None, min_length=1, max_length=120)
    employment_type: EmploymentTypeEnum | None = None
    summary: str | None = Field(None, min_length=1, max_length=400)
    description: str | None = Field(None, max_length=20000)
    status: JobStatusEnum | None = None
    slug: str | None = Field(None, max_length=160)

    # Every field below is NOT NULL on `job_postings`. They're typed nullable
    # only so they can be *omitted* from a partial update; an explicit null
    # would reach the INSERT and fail there, so it's a 422 here instead.
    # (`slug` is excluded: a null slug already means "keep the current one".)
    @field_validator(
        "title",
        "team",
        "location",
        "employment_type",
        "summary",
        "description",
        "status",
        mode="before",
    )
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


# ── Applications ────────────────────────────────────────────────────────


class JobApplicationCreate(BaseModel):
    """Public: submit an application to an open role."""

    applicant_name: str = Field(..., min_length=1, max_length=160)
    applicant_email: EmailStr
    linkedin_url: str | None = Field(None, max_length=500)
    resume_url: str | None = Field(None, max_length=1024)
    cover_letter: str | None = Field(None, max_length=8000)

    @field_validator("applicant_name", "linkedin_url", "resume_url", "cover_letter")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)


class ApplicationEventRead(BaseModel):
    """One entry in an application's status/communication trail."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: ApplicationStatusEnum
    message: str | None = None
    notified: bool = False
    created_at: datetime


class JobApplicationRead(BaseModel):
    """Admin: an application row (list view)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    applicant_name: str
    applicant_email: EmailStr
    linkedin_url: str | None = None
    resume_url: str | None = None
    cover_letter: str | None = None
    status: ApplicationStatusEnum
    created_at: datetime
    updated_at: datetime
    # Joined for convenience so the portal table needs no second fetch.
    job_title: str | None = None


class JobApplicationDetail(JobApplicationRead):
    """Admin: an application with its full event trail."""

    events: list[ApplicationEventRead] = Field(default_factory=list)


class ApplicationStatusUpdate(BaseModel):
    """Admin: advance an application and optionally message the applicant."""

    status: ApplicationStatusEnum
    message: str | None = Field(None, max_length=8000)
    # When true, the change is dispatched to the applicant (best-effort).
    notify: bool = True

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)


class ApplicationSubmitted(BaseModel):
    """Public response after applying — the reference the applicant tracks with."""

    id: uuid.UUID
    status: ApplicationStatusEnum
    job_title: str
    created_at: datetime


class ApplicationTrackRead(BaseModel):
    """Public: an applicant's view of their own application + updates."""

    id: uuid.UUID
    job_title: str
    applicant_name: str
    status: ApplicationStatusEnum
    created_at: datetime
    events: list[ApplicationEventRead] = Field(default_factory=list)


# ── Blog ────────────────────────────────────────────────────────────────


class BlogPostRead(BaseModel):
    """Full blog post (admin + public)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    excerpt: str
    body: str
    tag: str
    author: str
    cover_image_url: str | None = None
    read_minutes: int
    status: BlogStatusEnum
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BlogPostCreate(BaseModel):
    """Admin: create a post. `slug` derives from the title when omitted."""

    title: str = Field(..., min_length=2, max_length=200)
    excerpt: str = Field("", max_length=400)
    body: str = Field("", max_length=50000)
    tag: str = Field("Update", max_length=60)
    author: str = Field("The Loupe Team", max_length=120)
    cover_image_url: str | None = Field(None, max_length=1024)
    read_minutes: int = Field(3, ge=1, le=120)
    status: BlogStatusEnum = BlogStatusEnum.draft
    slug: str | None = Field(None, max_length=200)


class BlogPostUpdate(BaseModel):
    """Admin: partial update. Send only the fields that changed."""

    title: str | None = Field(None, min_length=2, max_length=200)
    excerpt: str | None = Field(None, max_length=400)
    body: str | None = Field(None, max_length=50000)
    tag: str | None = Field(None, max_length=60)
    author: str | None = Field(None, max_length=120)
    cover_image_url: str | None = Field(None, max_length=1024)
    read_minutes: int | None = Field(None, ge=1, le=120)
    status: BlogStatusEnum | None = None
    slug: str | None = Field(None, max_length=200)


__all__ = [
    "ApplicationEventRead",
    "ApplicationStatusUpdate",
    "ApplicationSubmitted",
    "ApplicationTrackRead",
    "BlogPostCreate",
    "BlogPostRead",
    "BlogPostUpdate",
    "JobApplicationCreate",
    "JobApplicationDetail",
    "JobApplicationRead",
    "JobPostingCreate",
    "JobPostingRead",
    "JobPostingUpdate",
    "slugify",
]
