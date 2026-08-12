"""Tests for the scanner speed + accuracy trend series (admin charts)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import pytest

from app.models.identification import CardIdentification
from app.services.admin import scanner_stats_service


def _ident(
    *, days_ago: int, source: str, latency: int, conf: float
) -> CardIdentification:
    return CardIdentification(
        image_sha256=uuid.uuid4().hex,
        ocr_provider="mock",
        tcg_inferred="pokemon",
        primary_source=source,
        top_confidence=conf,
        latency_ms=latency,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_trend_buckets_days_and_fills_gaps(db_session) -> None:
    # Two scans today (one fast pHash, one OCR), one scan 2 days ago.
    db_session.add_all(
        [
            _ident(days_ago=0, source="phash", latency=40, conf=0.95),
            _ident(days_ago=0, source="text", latency=600, conf=0.7),
            _ident(days_ago=2, source="phash", latency=50, conf=0.9),
        ]
    )
    await db_session.commit()

    trend = await scanner_stats_service.trend(db_session, days=7)

    assert trend.window_days == 7
    # A continuous x-axis: exactly ``days`` points, one per day, ending today.
    assert len(trend.points) == 7
    by_date = {p.date: p for p in trend.points}

    today = (datetime.now(UTC)).date().isoformat()
    two_ago = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()

    t = by_date[today]
    assert t.count == 2
    assert t.fast_path_rate == 0.5  # one of two was pHash
    assert t.latency_p50_ms > 0 and t.latency_p95_ms >= t.latency_p50_ms
    assert 0.7 <= t.mean_confidence <= 0.95

    assert by_date[two_ago].count == 1
    assert by_date[two_ago].fast_path_rate == 1.0

    # Empty days are present but zeroed (rendered as gaps, not dips).
    empties = [p for p in trend.points if p.count == 0]
    assert empties and all(p.mean_confidence == 0.0 for p in empties)


@pytest.mark.asyncio
async def test_trend_empty_window_is_all_zero_points(db_session) -> None:
    trend = await scanner_stats_service.trend(db_session, days=14)
    assert len(trend.points) == 14
    assert all(p.count == 0 for p in trend.points)


@pytest.mark.asyncio
async def test_trend_oldest_bucket_covers_a_whole_day(db_session) -> None:
    """Every bucket is a full UTC day, including the oldest one.

    The window is anchored to midnight rather than to the current clock time,
    so a scan from early on the first day of a 7-day window counts in full
    instead of falling into a partial bucket — and a scan from the day before
    the window is outside it entirely rather than half-counted.
    """
    first_day = (datetime.now(UTC) - timedelta(days=6)).date()
    just_inside = datetime.combine(first_day, time(0, 5), tzinfo=UTC)
    just_outside = just_inside - timedelta(hours=1)

    db_session.add_all(
        [
            CardIdentification(
                image_sha256=uuid.uuid4().hex,
                ocr_provider="mock",
                tcg_inferred="pokemon",
                primary_source="phash",
                top_confidence=0.9,
                latency_ms=42,
                created_at=when,
            )
            for when in (just_inside, just_outside)
        ]
    )
    await db_session.commit()

    trend = await scanner_stats_service.trend(db_session, days=7)

    assert len(trend.points) == 7
    assert trend.points[0].date == first_day.isoformat()
    assert trend.points[0].count == 1  # the 00:05 scan, not dropped
    # The scan from the previous day is outside the window, not in any bucket.
    assert sum(p.count for p in trend.points) == 1
