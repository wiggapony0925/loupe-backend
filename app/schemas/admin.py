"""Schemas for the admin developer portal — user management + metrics."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AdminUserRead(BaseModel):
    """A user row in the admin user table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime
    # DB-backed admin grant (what the toggle controls).
    is_admin: bool = False
    # Email is in ADMIN_EMAILS — always admin, can't be demoted/banned/deleted.
    is_super_admin: bool = False
    banned: bool = False
    banned_at: datetime | None = None
    ban_reason: str | None = None
    deleted: bool = False


class AdminUserDetail(AdminUserRead):
    """Full per-user record for the detail drawer — identity + activity."""

    updated_at: datetime | None = None
    # How the account authenticates: "password" | "apple" | "google" | "unknown".
    auth_method: str = "unknown"
    # Activity aggregates.
    grades_count: int = 0
    watchlist_count: int = 0
    scans_count: int = 0
    estimated_value_usd: float = 0.0


class AdminUserPage(BaseModel):
    """A page of users (server-side search + pagination)."""

    results: list[AdminUserRead]
    total: int
    page: int
    page_size: int


class TestAccountCreated(BaseModel):
    """A freshly-minted sandbox account — the password is shown only once."""

    id: uuid.UUID
    email: EmailStr
    password: str


class AdminRoleUpdate(BaseModel):
    """Grant or revoke the DB-backed admin flag."""

    is_admin: bool


class BanRequest(BaseModel):
    """Ban a user with an optional reason shown in the audit trail."""

    reason: str | None = Field(None, max_length=500)


class AdminMetrics(BaseModel):
    """At-a-glance portal metrics."""

    users_total: int
    users_new_7d: int
    users_new_30d: int
    admins: int
    banned: int
    jobs_total: int
    jobs_open: int
    applications_total: int
    applications_new_7d: int
    posts_total: int
    posts_published: int


__all__ = [
    "AdminMetrics",
    "AdminRoleUpdate",
    "AdminUserDetail",
    "AdminUserPage",
    "AdminUserRead",
    "BanRequest",
    "TestAccountCreated",
]
