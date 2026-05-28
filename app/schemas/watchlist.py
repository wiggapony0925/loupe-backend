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
    created_at: datetime
    card_name: str | None = None
    card_image_url: str | None = None


class WatchlistAdd(BaseModel):
    card_id: uuid.UUID


__all__ = ["WatchlistAdd", "WatchlistItemRead"]
