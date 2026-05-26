"""End-to-end tests for ``/v1/cards/identify`` + feedback.

We use the mock OCR provider (default) to canned-respond with a known
text payload and monkeypatch :func:`card_search_service.search_cards` so
the test never touches an upstream catalog. This keeps the test
hermetic + fast while still exercising the full router → pipeline →
persistence → response path.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from app.services.ocr import get_provider
from app.services.ocr.factory import reset_provider_cache
from app.services.ocr.mock import get_mock_provider
from tests.conftest import assert_envelope_ok


def _make_test_jpeg(text: str = "card") -> bytes:
    """Build a tiny but valid JPEG so Pillow + phash both succeed."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (240, 336), color=(220, 220, 220))
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 10, 230, 326), outline=(0, 0, 0), width=2)
    draw.text((20, 20), text, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture
def mock_search(monkeypatch):
    """Stub ``card_search_service.search_cards`` with a deterministic catalog."""

    async def fake_search_cards(*, q: str, tcg: str, limit: int = 20) -> dict[str, Any]:
        # Return the same Charizard payload regardless of query so the
        # pipeline's de-dupe + scoring + persistence paths get exercised.
        return {
            "results": [
                {
                    "id": "pokemontcg:base1-4",
                    "name": "Charizard",
                    "set": {"id": "base1", "code": "BS", "name": "Base Set"},
                    "number": "4/102",
                    "hp": "120",
                    "images": {"large": "https://example.test/charizard.png"},
                    "tcg": "pokemon",
                },
                {
                    "id": "pokemontcg:base1-5",
                    "name": "Charmeleon",
                    "set": {"id": "base1", "code": "BS", "name": "Base Set"},
                    "number": "24/102",
                    "hp": "80",
                    "images": {"large": "https://example.test/charmeleon.png"},
                    "tcg": "pokemon",
                },
            ],
            "total": 2,
            "source": "pokemontcg",
        }

    from app.services.catalog import card_search_service

    monkeypatch.setattr(card_search_service, "search_cards", fake_search_cards)
    return fake_search_cards


@pytest.fixture(autouse=True)
def _reset_mock_provider():
    """Each test gets a clean fixture map + provider cache."""
    reset_provider_cache()
    mock = get_mock_provider()
    mock.clear()
    yield
    mock.clear()
    reset_provider_cache()


@pytest.mark.asyncio
async def test_identify_returns_ranked_candidates(client, mock_search):
    image = _make_test_jpeg("Charizard")
    # Provider matches by sha256 of the *bytes the provider receives*,
    # which the pipeline rewrites via prepare_image_for_ocr. Use a
    # default so any image returns our canned text.
    from app.services.ocr.base import OcrBlock, OcrResult

    get_mock_provider().set_default(
        OcrResult(
            full_text="Charizard\nHP 120\nFire Spin\nBS 4/102\n",
            blocks=[OcrBlock(text="Charizard", confidence=0.95, bbox=(0, 0, 100, 20))],
            mean_confidence=0.95,
            language_codes=["en"],
            provider="mock",
            latency_ms=1,
        )
    )
    # Confirm the factory hands back the *same* mock instance.
    assert get_provider().name == "mock"

    resp = await client.post(
        "/v1/cards/identify",
        files={"image": ("card.jpg", image, "image/jpeg")},
        data={"tcg": "pokemon"},
    )
    body = assert_envelope_ok(resp)
    assert body["primary_source"] in {"text", "phash"}
    assert body["tcg_inferred"] == "pokemon"
    assert body["ocr_provider"] == "mock"
    assert body["cost_usd"] == 0.0
    assert body["candidates"]
    top = body["candidates"][0]
    assert top["name"] == "Charizard"
    assert top["confidence"] > 0.5
    assert top["upstream_id"] == "pokemontcg:base1-4"
    assert body["accuracy_score"] == top["confidence"]
    assert body["parsed"]["title"] == "Charizard"
    assert body["parsed"]["hp"] == 120


@pytest.mark.asyncio
async def test_identify_rejects_non_image_content_type(client, mock_search):
    resp = await client.post(
        "/v1/cards/identify",
        files={"image": ("card.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415


@pytest.mark.asyncio
async def test_identify_rejects_oversize_image(client, mock_search, monkeypatch):
    from app.config import get_settings

    # Patch the cached settings object so the size check fires on a small payload.
    settings = get_settings()
    monkeypatch.setattr(settings, "ocr_max_image_bytes", 100)
    image = _make_test_jpeg("X")
    resp = await client.post(
        "/v1/cards/identify",
        files={"image": ("big.jpg", image, "image/jpeg")},
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_feedback_requires_auth_and_persists(client, mock_search, auth_headers):
    from app.services.ocr.base import OcrBlock, OcrResult

    get_mock_provider().set_default(
        OcrResult(
            full_text="Charizard\nHP 120\n",
            blocks=[OcrBlock(text="Charizard", confidence=0.9, bbox=(0, 0, 1, 1))],
            mean_confidence=0.9,
            language_codes=["en"],
            provider="mock",
            latency_ms=1,
        )
    )
    image = _make_test_jpeg("Charizard")
    resp = await client.post(
        "/v1/cards/identify",
        files={"image": ("c.jpg", image, "image/jpeg")},
        data={"tcg": "pokemon"},
        headers=auth_headers,
    )
    body = assert_envelope_ok(resp)
    identification_id = body["identification_id"]

    # Unauthenticated feedback → 401.
    anon = await client.post(
        f"/v1/cards/identify/{identification_id}/feedback",
        json={"correct": True, "chosen_card_id": "pokemontcg:base1-4"},
    )
    assert anon.status_code == 401

    # Authenticated feedback → 204.
    ok = await client.post(
        f"/v1/cards/identify/{identification_id}/feedback",
        json={"correct": True, "chosen_card_id": "pokemontcg:base1-4"},
        headers=auth_headers,
    )
    assert ok.status_code == 204


@pytest.mark.asyncio
async def test_feedback_unknown_identification_returns_404(client, auth_headers):
    import uuid

    fake_id = uuid.uuid4()
    resp = await client.post(
        f"/v1/cards/identify/{fake_id}/feedback",
        json={"correct": True, "chosen_card_id": "pokemontcg:base1-4"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ocr_metrics_endpoint_returns_empty_window(client, auth_headers):
    resp = await client.get("/v1/cards/admin/ocr/metrics?days=7", headers=auth_headers)
    body = assert_envelope_ok(resp)
    assert body["window_days"] == 7
    assert body["total_identifications"] == 0
    assert body["total_feedback"] == 0
    assert body["top1_accuracy"] == 0.0
    assert body["latency_p50_ms"] == 0
