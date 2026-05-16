"""Card / CardSet schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TcgEnum


class CardSetRead(BaseModel):
    """Public representation of a card set."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tcg: TcgEnum
    name: str
    code: str | None = None
    release_date: date | None = None
    total_cards: int | None = None
    image_url: str | None = None


class CardRead(BaseModel):
    """Public representation of a card."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    set_id: uuid.UUID
    tcg: TcgEnum
    name: str
    number: str | None = None
    rarity: str | None = None
    year: int | None = None
    image_url: str | None = None
    metadata_: dict | None = Field(None, alias="card_metadata")
    created_at: datetime


class CardSearchQuery(BaseModel):
    """Query params bundle for ``GET /v1/cards``."""

    tcg: TcgEnum | None = None
    q: str | None = Field(None, max_length=120)
    set_code: str | None = Field(None, max_length=40)
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=100)


__all__ = ["CardRead", "CardSearchQuery", "CardSetRead"]
