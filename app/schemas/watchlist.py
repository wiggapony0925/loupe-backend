"""Watchlist wire schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WatchlistItemRead(BaseModel):
    """One pinned card. `card_name`/`card_image_url` joined for the UI."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    card_id: uuid.UUID
    #: The composite catalog id (`<source>:<external_id>`) the card was
    #: materialized from, e.g. `pokemontcg:base1-4`. Lets a client that only
    #: knows the upstream id (the browse/search view) match its "is this
    #: pinned?" state without first resolving to the local UUID.
    upstream_id: str | None = None
    created_at: datetime
    card_name: str | None = None
    card_image_url: str | None = None


class WatchlistAdd(BaseModel):
    #: A local Card UUID *or* a composite upstream id (`pokemontcg:base1-4`).
    #: The backend resolves + materializes it — the client passes whatever id
    #: the card view already has.
    card_id: str


__all__ = ["WatchlistAdd", "WatchlistItemRead"]
