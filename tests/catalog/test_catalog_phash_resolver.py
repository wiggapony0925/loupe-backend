"""Catalog perceptual-hash (pHash) matching tests.

Locks down :func:`card_resolver_service.resolve_catalog_by_phash`, the
bridge that lets a live scan's frame hash identify a *catalog* card by
Hamming distance over the indexed ``cards.image_phash`` column:

* an exact / near hash resolves to the catalog card,
* the closest card wins when several are indexed,
* a hash beyond ``phash_max_distance`` is rejected (no false positive),
* the feature is gated by the ``phash_enabled`` flag.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.config import get_settings
from app.models.card import Card
from app.models.enums import TcgEnum
from app.services.catalog import card_resolver_service
from tests.factories import make_card_set

# A real 16x16 imagehash renders to 64 hex chars (256 bits). These are
# synthetic but structurally valid: nibble 'a'=1010, 'b'=1011 differ by a
# single bit, 'f'=1111 differs from 'a' by two bits.
_BASE_HASH = "a" * 64
_NEAR_HASH = "a" * 63 + "b"  # Hamming distance 1 from _BASE_HASH
_FAR_HASH = "f" * 64  # Hamming distance 128 from _BASE_HASH


def _unified(name: str) -> dict:
    return {
        "id": f"pokemontcg:{name.lower()}",
        "name": name,
        "tcg": "pokemon",
        "image_url": "https://example/art.png",
    }


async def _make_card(
    db, *, name: str, phash: str, set_id: uuid.UUID | None = None
) -> Card:
    if set_id is None:
        cset = await make_card_set(db)
        set_id = cset.id
    card = Card(
        set_id=set_id,
        tcg=TcgEnum.pokemon,
        name=name,
        image_url="https://example/art.png",
        image_phash=phash,
        image_dhash=phash,
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


@pytest.mark.asyncio
async def test_exact_phash_resolves_catalog_card(db_session) -> None:
    card = await _make_card(db_session, name="Pikachu", phash=_BASE_HASH)
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_unified("Pikachu")),
    ):
        resolved = await card_resolver_service.resolve_catalog_by_phash(
            db_session, _BASE_HASH
        )
    assert resolved is not None
    assert resolved.card_id == card.id
    assert resolved.source == "fingerprint"
    assert resolved.confidence == 1.0


@pytest.mark.asyncio
async def test_near_phash_within_threshold_resolves(db_session) -> None:
    card = await _make_card(db_session, name="Pikachu", phash=_BASE_HASH)
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_unified("Pikachu")),
    ):
        # Query a frame hash 1 bit off the stored art — still a hit.
        resolved = await card_resolver_service.resolve_catalog_by_phash(
            db_session, _NEAR_HASH
        )
    assert resolved is not None
    assert resolved.card_id == card.id
    assert resolved.confidence < 1.0


@pytest.mark.asyncio
async def test_closest_catalog_card_wins(db_session) -> None:
    cset = await make_card_set(db_session)
    near = await _make_card(
        db_session, set_id=cset.id, name="Pikachu", phash=_BASE_HASH
    )
    await _make_card(db_session, set_id=cset.id, name="Charizard", phash=_FAR_HASH)
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_unified("Pikachu")),
    ):
        resolved = await card_resolver_service.resolve_catalog_by_phash(
            db_session, _NEAR_HASH
        )
    assert resolved is not None
    assert resolved.card_id == near.id


@pytest.mark.asyncio
async def test_far_phash_beyond_threshold_is_rejected(db_session) -> None:
    await _make_card(db_session, name="Charizard", phash=_FAR_HASH)
    resolved = await card_resolver_service.resolve_catalog_by_phash(
        db_session, _BASE_HASH
    )
    # 128-bit distance is far beyond phash_max_distance → no false positive.
    assert resolved is None


@pytest.mark.asyncio
async def test_phash_disabled_short_circuits(db_session) -> None:
    await _make_card(db_session, name="Pikachu", phash=_BASE_HASH)
    settings = get_settings()
    object.__setattr__(settings, "phash_enabled", False)
    try:
        resolved = await card_resolver_service.resolve_catalog_by_phash(
            db_session, _BASE_HASH
        )
    finally:
        object.__setattr__(settings, "phash_enabled", True)
    assert resolved is None


@pytest.mark.asyncio
async def test_empty_catalog_returns_none(db_session) -> None:
    resolved = await card_resolver_service.resolve_catalog_by_phash(
        db_session, _BASE_HASH
    )
    assert resolved is None
