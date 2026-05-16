"""Collection schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CollectionRead(BaseModel):
    """Public representation of a collection."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None = None
    color: str | None = None
    is_public: bool
    created_at: datetime
    updated_at: datetime


class CollectionCreate(BaseModel):
    """Body for ``POST /v1/collections``."""

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    color: str | None = Field(None, max_length=16)
    is_public: bool = False


class CollectionUpdate(BaseModel):
    """Body for ``PATCH /v1/collections/{id}``."""

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)
    color: str | None = Field(None, max_length=16)
    is_public: bool | None = None


class CollectionItemAdd(BaseModel):
    """Body for ``POST /v1/collections/{id}/items``."""

    graded_card_id: uuid.UUID


__all__ = [
    "CollectionCreate",
    "CollectionItemAdd",
    "CollectionRead",
    "CollectionUpdate",
]
