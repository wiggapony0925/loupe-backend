"""GradedCard schemas."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import GradeHouseEnum, RawConditionEnum

# Sanity bounds for user-supplied numbers / dates. Anything outside these
# almost certainly means a typo or a copy-paste error from a different
# field — better to 422 than to poison the portfolio.
_MAX_VALUE_USD = Decimal("10000000")  # 10M — well above any real card sale
_MIN_CARD_YEAR = 1900  # T206 Honus Wagner era ceiling
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _CTRL_RE.sub("", value).strip()
    return cleaned or None


# User organization tags — kept small + tidy so the vault filter UI stays clean.
_MAX_TAGS = 12
_MAX_TAG_LEN = 24


def _clean_tags(value: list[str] | None) -> list[str] | None:
    """Trim, drop empties/control chars, cap length + count, de-dupe (case-
    insensitive, first casing wins). ``None`` passes through so an update can
    distinguish "not provided" from "clear to none"."""
    if value is None:
        return None
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        tag = _CTRL_RE.sub("", raw).strip()[:_MAX_TAG_LEN].strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= _MAX_TAGS:
            break
    return out


class GradedCardRead(BaseModel):
    """Public representation of a graded card."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    card_id: uuid.UUID
    scan_job_id: uuid.UUID | None = None
    grade: Decimal
    house: GradeHouseEnum
    condition: RawConditionEnum | None = None
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = None
    # Cost basis (user-supplied). `null` means "no cost recorded" —
    # the UI hides P/L for that row rather than treating it as \$0.
    purchase_price_usd: Decimal | None = None
    purchase_date: date | None = None
    fingerprint_hash: str | None = None
    notes: str | None = None
    # User organization tags (e.g. "PC", "For sale"). Legacy rows store NULL;
    # coalesce to an empty list so clients never special-case null.
    tags: list[str] = []
    graded_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator("tags", mode="before")
    @classmethod
    def _tags_never_null(cls, v: object) -> object:
        return v or []

    # Joined card metadata so clients can render a row without an N+1 round-trip.
    card_name: str | None = None
    card_image_url: str | None = None
    card_number: str | None = None
    card_set_name: str | None = None
    card_year: int | None = None
    card_tcg: str | None = None
    # How many graded copies of THIS card_id the user owns in total
    # (including this row). Lets the vault badge "x3" without an extra
    # round-trip. Defaults to 1 — single copies are still 1, never 0.
    copies_owned: int = 1


class GradedCardCreate(BaseModel):
    """Body for manually creating a graded card (admin / import flows).

    Either ``card_id`` (an existing local UUID) or ``upstream_id`` (a
    composite ``<source>:<external_id>`` from a catalog hit / scan /
    deep link) must be supplied. When ``upstream_id`` is used, the
    server materializes the local :class:`Card` + :class:`CardExternalRef`
    via :func:`card_resolver_service.ensure_local_card` before inserting
    the grade — so the user never has to think about ids.
    """

    card_id: uuid.UUID | None = None
    upstream_id: str | None = Field(
        None,
        max_length=240,
        description="Composite catalog id like 'pokemontcg:base1-4'.",
    )
    grade: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10"))
    house: GradeHouseEnum = GradeHouseEnum.loupe
    condition: RawConditionEnum | None = Field(
        None,
        description=(
            "PSA-style raw condition (nm/lp/mp/hp/dmg). Only meaningful "
            "for ungraded (`house=loupe`) cards; ignored for slabbed grades."
        ),
    )
    subgrades: dict | None = None
    estimated_value_usd: Decimal | None = Field(
        None, ge=Decimal("0"), le=_MAX_VALUE_USD
    )
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"), le=_MAX_VALUE_USD)
    purchase_date: date | None = None
    notes: str | None = Field(None, max_length=2000)
    tags: list[str] | None = None
    scan_job_id: uuid.UUID | None = None
    fingerprint_hash: str | None = Field(None, max_length=128)

    # -------- validators ----------------------------------------------------

    @field_validator("tags")
    @classmethod
    def _clean_create_tags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_tags(v)

    @field_validator("upstream_id")
    @classmethod
    def _validate_upstream_id(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        # Must look like "<source>:<external_id>", no control chars, no
        # whitespace-injection.
        if ":" not in v or v.count(":") > 4:
            raise ValueError("upstream_id must be '<source>:<external_id>'")
        if _CTRL_RE.search(v) or any(c.isspace() for c in v):
            raise ValueError("upstream_id contains illegal characters")
        return v

    @field_validator("fingerprint_hash")
    @classmethod
    def _validate_fingerprint(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        if not _HEX_RE.match(v):
            raise ValueError("fingerprint_hash must be hexadecimal")
        return v.lower()

    @field_validator("purchase_date")
    @classmethod
    def _validate_purchase_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        today = date.today()
        if v > today:
            raise ValueError("purchase_date cannot be in the future")
        if v.year < _MIN_CARD_YEAR:
            raise ValueError(f"purchase_date year must be >= {_MIN_CARD_YEAR}")
        return v

    @field_validator("subgrades")
    @classmethod
    def _validate_subgrades(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("subgrades must be an object")
        for key, value in v.items():
            if not isinstance(key, str) or len(key) > 32:
                raise ValueError(f"subgrade key invalid: {key!r}")
            if not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"subgrade {key} must be a number")
            f = float(value)
            if not (0.0 <= f <= 10.0):
                raise ValueError(f"subgrade {key}={f} must be in [0, 10]")
        return v

    @field_validator("notes")
    @classmethod
    def _validate_notes(cls, v: str | None) -> str | None:
        return _strip_control_chars(v)


class GradedCardUpdate(BaseModel):
    """Body for ``PATCH /v1/grades/{id}``.

    Every field is optional — send only the ones you want to change. We
    accept ``grade`` / ``house`` / ``subgrades`` here (and not just the
    cost-basis trio) so the same form can be used to correct manually
    entered holdings, e.g. after the user gets a slab back from PSA.
    """

    grade: Decimal | None = Field(None, ge=Decimal("0"), le=Decimal("10"))
    house: GradeHouseEnum | None = None
    condition: RawConditionEnum | None = None
    subgrades: dict | None = None
    notes: str | None = Field(None, max_length=2000)
    estimated_value_usd: Decimal | None = Field(
        None, ge=Decimal("0"), le=_MAX_VALUE_USD
    )
    purchase_price_usd: Decimal | None = Field(None, ge=Decimal("0"), le=_MAX_VALUE_USD)
    purchase_date: date | None = None
    # `None` = leave tags unchanged; `[]` = clear all tags.
    tags: list[str] | None = None

    @field_validator("notes")
    @classmethod
    def _strip_notes(cls, v: str | None) -> str | None:
        return _strip_control_chars(v)

    @field_validator("tags")
    @classmethod
    def _clean_update_tags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_tags(v)

    @field_validator("purchase_date")
    @classmethod
    def _validate_purchase_date(cls, v: date | None) -> date | None:
        if v is None:
            return v
        if v > date.today():
            raise ValueError("purchase_date cannot be in the future")
        if v.year < _MIN_CARD_YEAR:
            raise ValueError(f"purchase_date year must be >= {_MIN_CARD_YEAR}")
        return v

    @field_validator("subgrades")
    @classmethod
    def _validate_subgrades(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("subgrades must be an object")
        for key, value in v.items():
            if not isinstance(key, str) or len(key) > 32:
                raise ValueError(f"subgrade key invalid: {key!r}")
            if not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"subgrade {key} must be a number")
            f = float(value)
            if not (0.0 <= f <= 10.0):
                raise ValueError(f"subgrade {key}={f} must be in [0, 10]")
        return v


__all__ = ["GradedCardCreate", "GradedCardRead", "GradedCardUpdate"]
