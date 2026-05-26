"""Tests for the OCR monthly-budget guardrail + /identify/text endpoint."""

from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.identification import CardIdentification
from app.services.identification.card_identifier import CardIdentifier
from app.services.ocr import budget as budget_mod


def _jpeg_bytes(color: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_budget_cache():
    budget_mod.reset_cache_for_tests()
    yield
    budget_mod.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_is_budget_exceeded_false_when_cap_disabled(
    db_session: AsyncSession, monkeypatch
):
    monkeypatch.setattr(get_settings(), "ocr_monthly_budget_usd", 0.0)
    assert await budget_mod.is_budget_exceeded(db_session) is False


@pytest.mark.asyncio
async def test_is_budget_exceeded_reads_mtd_sum(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "ocr_monthly_budget_usd", 1.0)
    db_session.add(
        CardIdentification(
            image_sha256="a" * 64,
            ocr_provider="google_vision",
            ocr_full_text="",
            ocr_confidence=0.0,
            tcg_inferred="pokemon",
            primary_source="none",
            top_confidence=0.0,
            candidates_json=[],
            latency_ms=10,
            cost_usd=0.5,
        )
    )
    await db_session.commit()
    assert await budget_mod.is_budget_exceeded(db_session) is False

    budget_mod.reset_cache_for_tests()
    db_session.add(
        CardIdentification(
            image_sha256="b" * 64,
            ocr_provider="google_vision",
            ocr_full_text="",
            ocr_confidence=0.0,
            tcg_inferred="pokemon",
            primary_source="none",
            top_confidence=0.0,
            candidates_json=[],
            latency_ms=10,
            cost_usd=0.75,
        )
    )
    await db_session.commit()
    assert await budget_mod.is_budget_exceeded(db_session) is True


@pytest.mark.asyncio
async def test_identify_short_circuits_when_budget_exceeded(
    db_session: AsyncSession, monkeypatch
):
    """When the budget is exhausted we must NOT call the paid provider."""
    monkeypatch.setattr(get_settings(), "ocr_monthly_budget_usd", 0.01)
    monkeypatch.setattr(get_settings(), "ocr_provider", "google_vision")

    db_session.add(
        CardIdentification(
            image_sha256="c" * 64,
            ocr_provider="google_vision",
            ocr_full_text="",
            ocr_confidence=0.0,
            tcg_inferred="pokemon",
            primary_source="none",
            top_confidence=0.0,
            candidates_json=[],
            latency_ms=10,
            cost_usd=1.00,
        )
    )
    await db_session.commit()
    budget_mod.reset_cache_for_tests()

    class _Boom:
        name = "google_vision"

        async def detect_text(self, *_a, **_kw):
            raise AssertionError("provider must not be called when over budget")

    identifier = CardIdentifier(provider=_Boom())
    outcome = await identifier.identify(db_session, image_bytes=_jpeg_bytes())
    assert outcome.fallback_required is True
    assert outcome.ocr.provider == "client_fallback"
    assert outcome.cost_usd == 0.0
    assert outcome.candidates == []


@pytest.mark.asyncio
async def test_identify_text_endpoint_runs_without_paid_ocr(client):
    """The text endpoint must reach the catalog without billing OCR."""
    resp = await client.post(
        "/v1/cards/identify/text",
        json={"text": "Pikachu HP 60", "tcg": "pokemon"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body.get("data", body)
    assert data["cost_usd"] == 0.0
    assert data["ocr_provider"] == "client_fallback"
    assert data["fallback_required"] is False
