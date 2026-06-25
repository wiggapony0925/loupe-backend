"""Schemas for the admin catalog-coverage surface.

Read-only aggregates over the card catalog: how much data backs each game, how
much of it is scanner-ready (has a perceptual hash), and where prices come from.
"""

from __future__ import annotations

from pydantic import BaseModel


class GameCoverage(BaseModel):
    tcg: str
    label: str
    sets: int
    cards: int
    # Cards with a perceptual hash — eligible for the scanner's pHash fast path.
    phash_cards: int
    phash_pct: float
    # False when the game is marketed/scaffolded but has no catalog data yet.
    backed: bool


class CatalogCoverage(BaseModel):
    total_cards: int
    total_sets: int
    phash_coverage_pct: float
    price_snapshots: int
    price_by_source: dict[str, int]
    games: list[GameCoverage]


__all__ = ["CatalogCoverage", "GameCoverage"]
