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


class CollectionItemsBulk(BaseModel):
    """Body for bulk add / remove / transfer of holdings into a collection.

    Cap keeps a single request O(n) with a hard upper bound so a buggy
    client can't fan out tens of thousands of joins in one round-trip.
    """

    graded_card_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=200)


class CollectionItemsTransfer(BaseModel):
    """Move holdings from ``source_id`` into the path collection (add + remove).

    Idempotent on the destination; missing source membership is a no-op
    for that id so partial selections stay safe.
    """

    source_id: uuid.UUID
    graded_card_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=200)


class CollectionItemsBulkResult(BaseModel):
    """How many memberships changed — clients refresh overview from this."""

    added: int = 0
    removed: int = 0


class CollectionMerge(BaseModel):
    """Body for ``POST /v1/collections/{id}/merge`` — fold ``source_id`` into
    the path collection, then delete the (now-empty) source."""

    source_id: uuid.UUID


class CollectionSummary(BaseModel):
    """One row of the portfolio switcher (the currency-style dropdown).

    ``id`` is null for the synthetic **All** entry — everything the user owns,
    which is derived (not a real row) and therefore never deletable. Custom
    collections are categorizations layered on top; deleting one drops the
    categorization, never the cards.
    """

    id: uuid.UUID | None
    name: str
    color: str | None = None
    card_count: int
    total_value_usd: float
    is_all: bool = False
    deletable: bool = True


__all__ = [
    "CollectionCreate",
    "CollectionItemAdd",
    "CollectionItemsBulk",
    "CollectionItemsBulkResult",
    "CollectionItemsTransfer",
    "CollectionMerge",
    "CollectionRead",
    "CollectionSummary",
    "CollectionUpdate",
]
