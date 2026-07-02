"""Tests for the admin scan-history log (list + detail)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.identification import CardIdentification, IdentificationFeedback
from app.services.admin import scan_history_service
from tests.factories import make_user


def _ident(
    *,
    minutes_ago: int,
    user_id: uuid.UUID | None = None,
    matched: bool = True,
    thumb: str | None = "QUJD",  # "ABC" base64
) -> CardIdentification:
    cands = (
        [
            {
                "card_id": None,
                "upstream_id": "pokemontcg:base1-4",
                "name": "Charizard",
                "confidence": 0.91,
                "source": "text",
                "breakdown": {},
            }
        ]
        if matched
        else []
    )
    return CardIdentification(
        user_id=user_id,
        image_sha256=uuid.uuid4().hex,
        phash="ffff0000ffff0000",
        ocr_provider="google_vision",
        ocr_full_text="Charizard 4/102 HP120",
        ocr_confidence=0.8,
        parsed_title="Charizard" if matched else None,
        parsed_card_number="4/102" if matched else None,
        tcg_inferred="pokemon",
        primary_source="text" if matched else "none",
        top_upstream_id="pokemontcg:base1-4" if matched else None,
        top_confidence=0.91 if matched else 0.0,
        candidates_json=cands,
        image_thumb_b64=thumb,
        latency_ms=540,
        cost_usd=0.0015,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_list_scans_newest_first_with_photo_and_account(db_session) -> None:
    user = await make_user(db_session, email="scanner@example.com")
    db_session.add_all(
        [
            _ident(minutes_ago=30, user_id=user.id, matched=True),
            _ident(minutes_ago=5, user_id=None, matched=False),  # newest, anon miss
        ]
    )
    await db_session.commit()

    page = await scan_history_service.list_scans(db_session, limit=40)

    assert page.total == 2
    assert page.next_cursor is None
    assert len(page.items) == 2

    # Newest first (the anonymous miss).
    miss = page.items[0]
    assert miss.user_id is None and miss.user_email is None
    assert miss.top_name is None and miss.candidate_count == 0
    assert miss.primary_source == "none"

    hit = page.items[1]
    assert hit.user_email == "scanner@example.com"
    assert hit.top_name == "Charizard"
    assert hit.top_upstream_id == "pokemontcg:base1-4"
    assert hit.candidate_count == 1
    # The scanned frame is a ready-to-render data URL.
    assert hit.image_url == "data:image/jpeg;base64,QUJD"
    assert hit.latency_ms == 540


@pytest.mark.asyncio
async def test_matched_filter_and_paging(db_session) -> None:
    db_session.add_all(
        [
            _ident(minutes_ago=10, matched=True),
            _ident(minutes_ago=9, matched=True),
            _ident(minutes_ago=8, matched=False),
        ]
    )
    await db_session.commit()

    only_hits = await scan_history_service.list_scans(db_session, matched=True)
    assert only_hits.total == 2
    assert all(i.top_upstream_id for i in only_hits.items)

    only_miss = await scan_history_service.list_scans(db_session, matched=False)
    assert only_miss.total == 1
    assert only_miss.items[0].top_upstream_id is None

    # Paging: first page of 2 (of 3) yields a next cursor at offset 2.
    first = await scan_history_service.list_scans(db_session, limit=2, offset=0)
    assert first.total == 3 and first.next_cursor == "2" and len(first.items) == 2
    second = await scan_history_service.list_scans(db_session, limit=2, offset=2)
    assert second.next_cursor is None and len(second.items) == 1


@pytest.mark.asyncio
async def test_get_scan_detail_includes_candidates_and_feedback(db_session) -> None:
    row = _ident(minutes_ago=1, matched=True)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    db_session.add(
        IdentificationFeedback(
            identification_id=row.id, correct=True, chosen_card_id="pokemontcg:base1-4"
        )
    )
    await db_session.commit()

    detail = await scan_history_service.get_scan(db_session, row.id)
    assert detail is not None
    assert detail.ocr_full_text == "Charizard 4/102 HP120"
    assert detail.parsed_set_code is None
    assert detail.phash == "ffff0000ffff0000"
    assert detail.feedback_correct is True
    assert len(detail.candidates) == 1
    assert detail.candidates[0].name == "Charizard"
    assert detail.candidates[0].confidence == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_get_scan_missing_returns_none(db_session) -> None:
    assert await scan_history_service.get_scan(db_session, uuid.uuid4()) is None
