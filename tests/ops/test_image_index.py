"""Tests for the catalog image-hash worker's stable-sweep paging.

The backfill script (:mod:`scripts.index_card_images`) walks the whole
``cards`` table via ``stable=True`` + ``offset``; these tests lock in that it
hashes every eligible card, skips already-hashed rows without re-downloading,
and never stalls on an image that fails to hash.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.card import Card
from app.models.enums import TcgEnum
from app.tasks import image_index
from tests.factories import make_card_set


def _fp(seed: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(phash=seed * 64, dhash=seed * 64)


async def _make_image_card(db, *, set_id, name, image_url):
    card = Card(
        set_id=set_id,
        tcg=TcgEnum.pokemon,
        name=name,
        image_url=image_url,
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


async def _sweep(batch_size: int) -> dict[str, int]:
    """Drive index_card_images like scripts.index_card_images does."""
    offset = 0
    totals = {"scanned": 0, "updated": 0, "missed": 0}
    while True:
        result = await image_index.index_card_images(
            batch_size=batch_size, stable=True, offset=offset
        )
        if result["scanned"] == 0:
            break
        for key in totals:
            totals[key] += result[key]
        offset += result["scanned"]
    return totals


@pytest.mark.asyncio
async def test_stable_sweep_hashes_all_cards(db_session, monkeypatch):
    cset = await make_card_set(db_session)
    for i in range(5):
        await _make_image_card(
            db_session, set_id=cset.id, name=f"Card {i}", image_url=f"http://img/{i}"
        )

    monkeypatch.setattr(
        image_index.fingerprint_service,
        "fingerprint_from_image_url",
        AsyncMock(return_value=_fp("a")),
    )

    totals = await _sweep(batch_size=2)

    assert totals["updated"] == 5
    assert totals["missed"] == 0
    rows = (await db_session.execute(select(Card.image_phash))).scalars().all()
    assert all(h == "a" * 64 for h in rows)


@pytest.mark.asyncio
async def test_stable_sweep_skips_already_hashed(db_session, monkeypatch):
    cset = await make_card_set(db_session)
    hashed = await _make_image_card(
        db_session, set_id=cset.id, name="Hashed", image_url="http://img/done"
    )
    hashed.image_phash = "b" * 64
    hashed.image_dhash = "b" * 64
    await db_session.commit()
    await _make_image_card(
        db_session, set_id=cset.id, name="Fresh", image_url="http://img/fresh"
    )

    fp_mock = AsyncMock(return_value=_fp("c"))
    monkeypatch.setattr(
        image_index.fingerprint_service, "fingerprint_from_image_url", fp_mock
    )

    totals = await _sweep(batch_size=10)

    # Only the un-hashed card is downloaded + updated; the hashed one is
    # skipped in-loop without a network call.
    assert totals["updated"] == 1
    assert fp_mock.await_count == 1


@pytest.mark.asyncio
async def test_stable_sweep_does_not_loop_on_unhashable(db_session, monkeypatch):
    cset = await make_card_set(db_session)
    for i in range(3):
        await _make_image_card(
            db_session, set_id=cset.id, name=f"Bad {i}", image_url=f"http://img/bad{i}"
        )

    # Every image fails to hash -> image_phash stays NULL. A naive NULL-filter
    # loop would re-select these forever; the stable offset sweep terminates.
    monkeypatch.setattr(
        image_index.fingerprint_service,
        "fingerprint_from_image_url",
        AsyncMock(return_value=None),
    )

    totals = await _sweep(batch_size=1)

    assert totals["missed"] == 3
    assert totals["updated"] == 0
