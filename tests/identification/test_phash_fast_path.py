"""pHash fast path: a near-exact catalog art-hash match must skip OCR.

This is the speed/cost win — when a scanned frame's perceptual hash is within
the tight fast-path distance of an indexed catalog card, identification returns
immediately without ever calling the (slow, paid) Vision provider.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from app.config import get_settings
from app.models.card import Card
from app.models.enums import TcgEnum
from app.services.catalog import card_resolver_service
from app.services.identification.card_identifier import CardIdentifier
from app.services.identification.image_ops import prepare_image_for_ocr
from tests.factories import make_card_set


def _jpeg_bytes(color: tuple[int, int, int] = (180, 90, 40)) -> bytes:
    buf = io.BytesIO()
    # A little structure so the perceptual hash isn't fully degenerate.
    img = Image.new("RGB", (120, 168), color)
    for y in range(0, 168, 12):
        for x in range(0, 120, 12):
            if (x + y) % 24 == 0:
                for dy in range(12):
                    for dx in range(12):
                        img.putpixel((x + dx, y + dy), (20, 20, 20))
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _BoomProvider:
    """OCR provider that explodes if called — proves we skipped OCR."""

    name = "google_vision"

    async def detect_text(self, *_a, **_kw):
        raise AssertionError("OCR must not run on a pHash fast-path hit")


@pytest.mark.asyncio
async def test_phash_fast_path_skips_ocr(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ocr_provider", "google_vision")
    image = _jpeg_bytes()
    prepared = prepare_image_for_ocr(image)
    assert prepared.fingerprint is not None
    phash = prepared.fingerprint.phash
    assert phash

    # Seed a catalog card whose art hash exactly matches the frame (distance 0).
    cset = await make_card_set(db_session)
    card = Card(
        set_id=cset.id,
        tcg=TcgEnum.pokemon,
        name="Pikachu",
        image_url="https://example/art.png",
        image_phash=phash,
        image_dhash=phash,
    )
    db_session.add(card)
    await db_session.commit()
    await db_session.refresh(card)

    unified = {
        "id": "pokemontcg:base1-58",
        "card_id": str(card.id),
        "name": "Pikachu",
        "tcg": "pokemon",
        "image_url": "https://example/art.png",
    }

    identifier = CardIdentifier(provider=_BoomProvider())
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=unified),
    ):
        outcome = await identifier.identify(db_session, image_bytes=image)

    assert outcome.primary_source == "phash"
    assert outcome.ocr.provider == "phash_fast_path"
    assert outcome.cost_usd == 0.0
    assert outcome.candidates
    assert outcome.candidates[0].name == "Pikachu"
    assert outcome.candidates[0].confidence >= 0.9


@pytest.mark.asyncio
async def test_no_phash_match_still_runs_ocr(db_session, monkeypatch):
    """With nothing in the catalog index, the normal OCR path must run."""
    monkeypatch.setattr(get_settings(), "ocr_provider", "google_vision")

    called = {"ocr": False}

    class _Provider:
        name = "google_vision"

        async def detect_text(self, *_a, **_kw):
            from app.services.ocr import OcrResult

            called["ocr"] = True
            return OcrResult(
                full_text="Pikachu",
                blocks=[],
                mean_confidence=0.5,
                language_codes=["en"],
                provider="google_vision",
                latency_ms=5,
            )

    identifier = CardIdentifier(provider=_Provider())
    outcome = await identifier.identify(db_session, image_bytes=_jpeg_bytes())
    assert called["ocr"] is True
    assert outcome.ocr.provider == "google_vision"
