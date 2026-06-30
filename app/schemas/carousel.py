"""Carousel recipe schema — the serializable contract for a marketplace shelf.

A *recipe* is a fully data-driven carousel definition over a constrained filter
vocabulary (price band, rarity, sort). It is what the AI generator emits and
what the web compiles into a real rail. Keeping it to a small, validated
vocabulary means an LLM can invent creative shelves safely: it picks the theme,
copy, and filters, but the cards still come from our own cached data.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CarouselRecipe(BaseModel):
    """One AI- or curator-authored carousel. Mirrors the web `CarouselRecipe`."""

    id: str = Field(max_length=48)
    title: str = Field(max_length=60)
    subtitle: str = Field(max_length=120)
    #: Where the cards come from. value/trending use the priced discovery pools;
    #: catalog pulls from the browse catalog (works for catalog-only games).
    source: Literal["value", "trending", "catalog"] = "value"
    priceMin: float | None = Field(default=None, ge=0, le=100_000)
    priceMax: float | None = Field(default=None, ge=0, le=100_000)
    #: Rarity regex source, e.g. "secret|rainbow|illustration".
    rarityPattern: str | None = Field(default=None, max_length=120)
    rarities: list[str] | None = Field(default=None, max_length=12)
    sort: Literal["price_desc", "price_asc", "name"] | None = "price_desc"
    limit: int | None = Field(default=20, ge=4, le=40)
    minItems: int | None = Field(default=4, ge=1, le=12)


class CarouselResponse(BaseModel):
    """A game's generated shelves + where they came from (for the UI badge)."""

    game: str
    source: Literal["ai", "curated"]
    carousels: list[CarouselRecipe]
