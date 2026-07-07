"""User-facing Pydantic schemas (Read / Update + settings)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRead(BaseModel):
    """Public representation of a user (e.g. ``GET /me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: datetime
    # Whether they've proven ownership of the address (verify link clicked,
    # or a social provider vouched for it). Informational — nothing gated.
    email_verified: bool = False
    # Whether this user is in the admin allowlist — drives access to the
    # developer portal in the clients. Computed server-side; never trusted
    # from the client. Set by the `/me` handler from the email allowlist.
    is_admin: bool = False


class UserUpdate(BaseModel):
    """Allowed mutations on the caller's own user record."""

    display_name: str | None = Field(None, max_length=120)
    avatar_url: str | None = Field(None, max_length=1024)


class UserSettingsRead(BaseModel):
    """Per-user settings as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    currency: str = "USD"
    theme: str = "system"
    live_sync_enabled: bool = True
    push_notifications_enabled: bool = True
    email_announcements_enabled: bool = True
    updated_at: datetime | None = None


class UserSettingsUpdate(BaseModel):
    """Allowed mutations on user settings."""

    # ISO-4217 fiat codes (USD, EUR…) plus crypto tickers (BTC, USDC, MATIC) —
    # the clients share one display-currency catalog, so codes run 2-6 chars.
    currency: str | None = Field(
        default=None, min_length=2, max_length=6, pattern=r"^[A-Za-z]+$"
    )
    theme: str | None = Field(default=None, pattern="^(system|light|dark)$")
    live_sync_enabled: bool | None = None
    push_notifications_enabled: bool | None = None
    email_announcements_enabled: bool | None = None


__all__ = ["UserRead", "UserSettingsRead", "UserSettingsUpdate", "UserUpdate"]
