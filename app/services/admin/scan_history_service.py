"""Scan history log for the admin portal.

Every ``POST /v1/cards/identify`` persists a :class:`CardIdentification` row —
the scanned frame (thumbnail), who scanned it, when, the parsed OCR, and the
ranked candidates we returned. This service reads that log back for the dev
portal: a paginated, filterable feed plus a per-scan drill-down. Read-only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identification import CardIdentification, IdentificationFeedback
from app.models.user import User
from app.schemas.scanner_stats import (
    ScanHistoryCandidate,
    ScanHistoryDetail,
    ScanHistoryItem,
    ScanHistoryPage,
)

# Hard cap so a single page can never pull an unbounded number of (thumbnail-
# carrying) rows.
_MAX_LIMIT = 100


def _data_url(thumb_b64: str | None) -> str | None:
    """Wrap a stored base64 JPEG as a ready-to-render data URL."""
    return f"data:image/jpeg;base64,{thumb_b64}" if thumb_b64 else None


def _iso(dt: datetime) -> str:
    """UTC ISO-8601. ``created_at`` may be naive on SQLite — assume UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _apply_filters(
    stmt: Select[Any],
    *,
    user_id: uuid.UUID | None,
    source: str | None,
    tcg: str | None,
    provider: str | None,
    min_confidence: float | None,
    matched: bool | None,
) -> Select[Any]:
    if user_id is not None:
        stmt = stmt.where(CardIdentification.user_id == user_id)
    if source:
        stmt = stmt.where(CardIdentification.primary_source == source)
    if tcg:
        stmt = stmt.where(CardIdentification.tcg_inferred == tcg)
    if provider:
        stmt = stmt.where(CardIdentification.ocr_provider == provider)
    if min_confidence is not None:
        stmt = stmt.where(CardIdentification.top_confidence >= min_confidence)
    if matched is True:
        stmt = stmt.where(CardIdentification.top_upstream_id.is_not(None))
    elif matched is False:
        stmt = stmt.where(CardIdentification.top_upstream_id.is_(None))
    return stmt


def _to_item(
    ci: CardIdentification, email: str | None, feedback_correct: bool | None
) -> ScanHistoryItem:
    cands: list[dict[str, Any]] = ci.candidates_json or []
    top = cands[0] if cands else {}
    return ScanHistoryItem(
        id=str(ci.id),
        created_at=_iso(ci.created_at),
        user_id=str(ci.user_id) if ci.user_id else None,
        user_email=email,
        image_url=_data_url(ci.image_thumb_b64),
        top_name=top.get("name") or None,
        top_upstream_id=ci.top_upstream_id,
        top_confidence=float(ci.top_confidence or 0.0),
        primary_source=ci.primary_source,
        candidate_count=len(cands),
        tcg_inferred=ci.tcg_inferred,
        ocr_provider=ci.ocr_provider,
        parsed_title=ci.parsed_title,
        parsed_number=ci.parsed_card_number,
        latency_ms=int(ci.latency_ms or 0),
        cost_usd=float(ci.cost_usd or 0.0),
        feedback_correct=feedback_correct,
    )


async def _feedback_map(
    db: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """Latest thumbs-up/down per identification for the given ids."""
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                IdentificationFeedback.identification_id,
                IdentificationFeedback.correct,
            )
            .where(IdentificationFeedback.identification_id.in_(ids))
            .order_by(IdentificationFeedback.created_at.desc())
        )
    ).all()
    out: dict[uuid.UUID, bool] = {}
    for iid, correct in rows:
        out.setdefault(iid, correct)  # first seen = latest (desc order)
    return out


async def list_scans(
    db: AsyncSession,
    *,
    limit: int = 40,
    offset: int = 0,
    user_id: uuid.UUID | None = None,
    source: str | None = None,
    tcg: str | None = None,
    provider: str | None = None,
    min_confidence: float | None = None,
    matched: bool | None = None,
) -> ScanHistoryPage:
    """A newest-first page of scans with the scanned frame + metadata."""
    limit = max(1, min(_MAX_LIMIT, limit))
    offset = max(0, offset)

    total = (
        await db.execute(
            _apply_filters(
                select(func.count()).select_from(CardIdentification),
                user_id=user_id,
                source=source,
                tcg=tcg,
                provider=provider,
                min_confidence=min_confidence,
                matched=matched,
            )
        )
    ).scalar_one()

    rows = (
        await db.execute(
            _apply_filters(
                select(CardIdentification, User.email).outerjoin(
                    User, User.id == CardIdentification.user_id
                ),
                user_id=user_id,
                source=source,
                tcg=tcg,
                provider=provider,
                min_confidence=min_confidence,
                matched=matched,
            )
            .order_by(
                CardIdentification.created_at.desc(), CardIdentification.id.desc()
            )
            .offset(offset)
            .limit(limit)
        )
    ).all()

    fb = await _feedback_map(db, [ci.id for ci, _ in rows])
    items = [_to_item(ci, email, fb.get(ci.id)) for ci, email in rows]
    next_offset = offset + limit
    return ScanHistoryPage(
        items=items,
        next_cursor=str(next_offset) if next_offset < int(total) else None,
        total=int(total),
    )


async def get_scan(db: AsyncSession, scan_id: uuid.UUID) -> ScanHistoryDetail | None:
    """Full drill-down for one scan: every candidate + the raw OCR text."""
    row = (
        await db.execute(
            select(CardIdentification, User.email)
            .outerjoin(User, User.id == CardIdentification.user_id)
            .where(CardIdentification.id == scan_id)
        )
    ).first()
    if row is None:
        return None
    ci, email = row

    fb = (
        await db.execute(
            select(IdentificationFeedback.correct)
            .where(IdentificationFeedback.identification_id == ci.id)
            .order_by(IdentificationFeedback.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    item = _to_item(ci, email, fb)
    candidates = [
        ScanHistoryCandidate(
            upstream_id=c.get("upstream_id"),
            card_id=c.get("card_id"),
            name=c.get("name") or "",
            confidence=float(c.get("confidence") or 0.0),
            source=c.get("source") or "",
        )
        for c in (ci.candidates_json or [])
    ]
    return ScanHistoryDetail(
        **item.model_dump(),
        ocr_full_text=ci.ocr_full_text or None,
        ocr_confidence=float(ci.ocr_confidence or 0.0),
        parsed_set_code=ci.parsed_set_code,
        phash=ci.phash,
        image_sha256=ci.image_sha256,
        candidates=candidates,
    )


__all__ = ["get_scan", "list_scans"]
