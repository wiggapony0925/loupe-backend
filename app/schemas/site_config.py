"""Schemas for the admin site config — Pro plan shape + announcement banner."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Partial-update schemas type their fields `X | None` so they can be omitted.
# For fields backed by a NOT NULL column an *explicit* null is not a partial
# update — it's a write the DB will refuse — so it is rejected here (a 422)
# rather than at the INSERT (a 500). Omitting the field still means "leave it".
_NULL_NOT_ALLOWED = "must not be null; omit the field to leave it unchanged"


class PlanConfigRead(BaseModel):
    """The Loupe Pro plan shape the admin controls live."""

    model_config = ConfigDict(from_attributes=True)

    # null = unlimited free.
    free_card_limit: int | None = None
    free_statement_limit: int | None = None
    # True = Pro-only; False = free for everyone.
    gate_unlimited_cards: bool = True
    gate_scanner_import: bool = True
    gate_full_history: bool = True
    gate_unlimited_alerts: bool = True
    gate_statements: bool = True


class PlanConfigUpdate(BaseModel):
    free_card_limit: int | None = Field(default=None, ge=0, le=100000)
    free_statement_limit: int | None = Field(default=None, ge=0, le=100000)
    gate_unlimited_cards: bool | None = None
    gate_scanner_import: bool | None = None
    gate_full_history: bool | None = None
    gate_unlimited_alerts: bool | None = None
    gate_statements: bool | None = None
    # Allow explicitly setting a limit back to "unlimited" (null). Because the
    # fields above can't distinguish "unset" from "null", these flags request it.
    clear_card_limit: bool = False
    clear_statement_limit: bool = False

    # The limits above are genuinely nullable ("unlimited"); the gates are not.
    @field_validator(
        "gate_unlimited_cards",
        "gate_scanner_import",
        "gate_full_history",
        "gate_unlimited_alerts",
        "gate_statements",
        mode="before",
    )
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError(_NULL_NOT_ALLOWED)
        return value


class AnnouncementRead(BaseModel):
    """Global banner shown to every user when enabled."""

    model_config = ConfigDict(from_attributes=True)

    enabled: bool = False
    message: str = ""
    tone: str = "info"
    cta_label: str | None = None
    cta_href: str | None = None


class AnnouncementUpdate(BaseModel):
    enabled: bool | None = None
    message: str | None = Field(default=None, max_length=500)
    tone: str | None = Field(default=None, pattern="^(info|success|warning|error)$")
    cta_label: str | None = Field(default=None, max_length=80)
    cta_href: str | None = Field(default=None, max_length=2000)

    # `cta_label`/`cta_href` are nullable columns — null clears the CTA. The
    # three fields below are NOT NULL, so null is a client error, not a clear.
    @field_validator("enabled", "message", "tone", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError(_NULL_NOT_ALLOWED)
        return value


class SiteConfigRead(BaseModel):
    """Everything the developer-portal config page reads in one shot."""

    plan: PlanConfigRead
    announcement: AnnouncementRead
    updated_at: datetime | None = None


__all__ = [
    "AnnouncementRead",
    "AnnouncementUpdate",
    "PlanConfigRead",
    "PlanConfigUpdate",
    "SiteConfigRead",
]
