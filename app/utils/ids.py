"""UUID helper utilities."""

from __future__ import annotations

import uuid


def new_uuid() -> uuid.UUID:
    """Return a fresh random UUID (v4)."""
    return uuid.uuid4()


def parse_uuid(value: str | uuid.UUID) -> uuid.UUID:
    """Coerce *value* to a :class:`uuid.UUID`; raise ``ValueError`` if malformed."""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


__all__ = ["new_uuid", "parse_uuid"]
