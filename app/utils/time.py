"""Timezone-aware datetime helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware :class:`datetime`."""
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    """Return *value* as an RFC 3339 / ISO 8601 string in UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


__all__ = ["to_iso", "utcnow"]
