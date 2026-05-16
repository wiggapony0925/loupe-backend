"""Shared Pydantic schemas: pagination, error envelope."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorEnvelope(BaseModel):
    """Standard error response body returned by failure paths."""

    model_config = ConfigDict(from_attributes=True)

    detail: str = Field(..., description="Human-readable error description.")
    code: str | None = Field(None, description="Machine-readable error code, optional.")


class Pagination(BaseModel, Generic[T]):
    """Generic paginated response envelope."""

    model_config = ConfigDict(from_attributes=True)

    items: list[T] = Field(default_factory=list, description="Page of items.")
    total: int = Field(0, description="Total number of items across all pages.")
    page: int = Field(1, ge=1, description="1-based page index.")
    page_size: int = Field(25, ge=1, le=100, description="Items per page.")


__all__ = ["ErrorEnvelope", "Pagination"]
