"""The universal API response envelope (and helpers to build one).

Every HTTP JSON response emitted by the FastAPI app — success or failure —
is wrapped in the :class:`Envelope` shape::

    {
      "data": <T> | null,
      "meta": { "request_id", "timestamp", "version", "duration_ms" },
      "pagination": Pagination | null,
      "error": ErrorDetail | null
    }

See ``CONTRACT.md`` at the repo root for the full specification.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.request_context import (
    get_request_id,
    new_request_id,
    request_elapsed_ms,
)
from app.utils.time import utcnow

API_VERSION = "v1"

T = TypeVar("T")


def _iso(value: datetime) -> str:
    """Serialise *value* as ISO-8601 UTC with millisecond precision + trailing ``Z``."""
    if value.tzinfo is None:
        from datetime import UTC

        value = value.replace(tzinfo=UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Meta(BaseModel):
    """Envelope metadata block — present on every response."""

    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(
        ..., description="ULID-ish per-request token for log correlation."
    )
    timestamp: str = Field(
        ..., description="ISO 8601 UTC timestamp when the envelope was built."
    )
    version: str = Field(default=API_VERSION, description="API contract version.")
    duration_ms: int | None = Field(
        default=None, description="Server-side request duration in milliseconds."
    )


class Pagination(BaseModel):
    """Pagination block, present only on list responses."""

    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1, le=100)
    total: int = Field(..., ge=0)
    total_pages: int = Field(..., ge=0)
    has_next: bool
    has_prev: bool
    next_cursor: str | None = None
    prev_cursor: str | None = None


class ErrorDetail(BaseModel):
    """Structured error payload returned on 4xx/5xx responses."""

    code: str = Field(..., description="Dot-namespaced machine-readable error code.")
    message: str = Field(..., description="Human-readable error message.")
    status: int = Field(..., ge=100, le=599)
    field: str | None = Field(
        default=None,
        description="Field name (for validation errors), else ``None``.",
    )
    details: Any | None = Field(
        default=None, description="Additional structured context for the error."
    )


class Envelope(BaseModel, Generic[T]):
    """Universal API response envelope (generic over the payload type ``T``)."""

    data: T | None = None
    meta: Meta
    pagination: Pagination | None = None
    error: ErrorDetail | None = None


# ----------------------------------------------------------------- helpers


def build_meta(*, duration_ms_override: int | None = None) -> Meta:
    """Build a :class:`Meta` block from the current request context."""
    rid = get_request_id() or new_request_id()
    elapsed = (
        duration_ms_override
        if duration_ms_override is not None
        else request_elapsed_ms()
    )
    return Meta(
        request_id=rid,
        timestamp=_iso(utcnow()),
        version=API_VERSION,
        duration_ms=elapsed,
    )


def build_pagination(*, total: int, page: int, page_size: int) -> Pagination:
    """Compute pagination metadata for the given counts."""
    page = max(1, page)
    page_size = max(1, page_size)
    total = max(0, total)
    total_pages = (total + page_size - 1) // page_size if page_size else 0
    return Pagination(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_prev=page > 1,
        next_cursor=None,
        prev_cursor=None,
    )


def ok(  # noqa: UP047
    data: T,
    *,
    pagination: Pagination | None = None,
) -> Envelope[T]:
    """Build a success envelope around *data*."""
    return Envelope[T](data=data, meta=build_meta(), pagination=pagination)


def page(  # noqa: UP047
    items: list[T],
    *,
    total: int,
    page: int,
    page_size: int,
) -> Envelope[list[T]]:
    """Build a paginated success envelope around *items*."""
    return Envelope[list[T]](
        data=items,
        meta=build_meta(),
        pagination=build_pagination(total=total, page=page, page_size=page_size),
    )


def fail(
    code: str,
    message: str,
    status: int,
    *,
    field: str | None = None,
    details: Any | None = None,
) -> Envelope[None]:
    """Build an error envelope (``data`` is always ``None``)."""
    return Envelope[None](
        data=None,
        meta=build_meta(),
        error=ErrorDetail(
            code=code,
            message=message,
            status=status,
            field=field,
            details=details,
        ),
    )


__all__ = [
    "API_VERSION",
    "Envelope",
    "ErrorDetail",
    "Meta",
    "Pagination",
    "build_meta",
    "build_pagination",
    "fail",
    "ok",
    "page",
]
