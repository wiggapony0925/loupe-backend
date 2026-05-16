"""Per-request context (ULID, start time) exposed via :mod:`contextvars`.

Populated by :class:`~app.http_middleware.RequestLogMiddleware` and read by
the envelope helpers (:mod:`app.schemas.envelope`) and the global exception
handlers (:mod:`app.exception_handlers`).
"""

from __future__ import annotations

import secrets
import time
from contextvars import ContextVar

# 16-byte hex (32 chars) — close enough to a ULID for log correlation and
# substantially shorter than a UUIDv4 in logs.
_request_id: ContextVar[str | None] = ContextVar("loupe_request_id", default=None)
_request_started_at: ContextVar[float | None] = ContextVar(
    "loupe_request_started_at", default=None
)
_request_user_id: ContextVar[str | None] = ContextVar(
    "loupe_request_user_id", default=None
)


def new_request_id() -> str:
    """Return a fresh request-id string (24-char URL-safe token)."""
    return secrets.token_hex(12)


def set_request_id(value: str) -> None:
    """Bind *value* as the current request-id."""
    _request_id.set(value)


def get_request_id() -> str | None:
    """Return the current request-id, if any."""
    return _request_id.get()


def set_request_started_at(value: float) -> None:
    """Bind *value* (monotonic seconds) as the current request start time."""
    _request_started_at.set(value)


def get_request_started_at() -> float | None:
    """Return the monotonic start time of the current request, if any."""
    return _request_started_at.get()


def set_request_user_id(value: str | None) -> None:
    """Bind the authenticated user-id for the current request, if any."""
    _request_user_id.set(value)


def get_request_user_id() -> str | None:
    """Return the authenticated user-id for the current request, if any."""
    return _request_user_id.get()


def request_elapsed_ms() -> int | None:
    """Return the elapsed milliseconds since the current request started."""
    started = get_request_started_at()
    if started is None:
        return None
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "get_request_id",
    "get_request_started_at",
    "get_request_user_id",
    "new_request_id",
    "request_elapsed_ms",
    "set_request_id",
    "set_request_started_at",
    "set_request_user_id",
]
