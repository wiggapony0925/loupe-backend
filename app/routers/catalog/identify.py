"""``/cards/identify*`` endpoints — image → ranked card candidates.

Three public surfaces:

* ``POST /v1/cards/identify`` — multipart image upload, returns the top-N
  candidates plus an ``identification_id`` the client uses for feedback.
  Auth is *optional* (matches the public ``GET /cards/search`` posture)
  so the camera-first onboarding flow works pre-login.
* ``POST /v1/cards/identify/{id}/feedback`` — thumbs-up/down + optional
  correction. Auth required (we want to attribute the signal).
* ``GET /v1/cards/admin/ocr/metrics`` — rolling accuracy + cost
  dashboard. Auth required; admin gating is TODO (no role model yet).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import optional_user, require_user
from app.config import get_settings
from app.db import get_db
from app.models.identification import CardIdentification, IdentificationFeedback
from app.models.user import User
from app.rate_limit import rate_limit
from app.schemas.identification import (
    IdentifyCandidate,
    IdentifyFeedbackRequest,
    IdentifyMetricsResponse,
    IdentifyParsed,
    IdentifyResponse,
)
from app.services.identification.card_identifier import CardIdentifier
from app.utils.logger import get_logger

logger = get_logger("routers.cards.identify")

router = APIRouter(prefix="/cards", tags=["cards"])

# Tight on the identify endpoint — Vision calls are expensive and we
# rate-limit per-user / per-IP rather than per-route globally.
identify_limit = rate_limit(limit=30, window_seconds=60, name="cards.identify")
feedback_limit = rate_limit(limit=60, window_seconds=60, name="cards.identify.feedback")


@router.post(
    "/identify",
    response_model=IdentifyResponse,
    summary="Identify a card from a photo",
    dependencies=[Depends(identify_limit)],
)
async def identify_card(
    image: UploadFile = File(..., description="JPEG/PNG photo of a single card."),
    tcg: str | None = Form(
        default=None,
        description='Optional hint: "pokemon" | "magic" | "yugioh" | "onepiece" | "lorcana" | "sports".',
    ),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> IdentifyResponse:
    """Run OCR + catalog ranking against the uploaded photo.

    Always 200 unless the image is missing / unreadable / oversize.
    Upstream OCR or catalog errors are absorbed by the pipeline and
    surfaced as an empty / low-confidence candidate list.
    """
    settings = get_settings()
    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=415, detail="image content-type required")

    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty image upload")
    if len(raw) > settings.ocr_max_image_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"image exceeds {settings.ocr_max_image_bytes} bytes",
        )

    identifier = CardIdentifier()
    try:
        outcome = await identifier.identify(
            db,
            image_bytes=raw,
            tcg_hint=tcg,
            user_id=user.id if user else None,
        )
    except ValueError as exc:
        # Pillow raises on truly malformed images; everything else is
        # already swallowed inside the pipeline.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return IdentifyResponse(
        identification_id=outcome.identification_id,
        candidates=[
            IdentifyCandidate(**dataclasses.asdict(c)) for c in outcome.candidates
        ],
        accuracy_score=outcome.accuracy_score,
        primary_source=outcome.primary_source,
        tcg_inferred=outcome.tcg_inferred,
        parsed=IdentifyParsed(
            title=outcome.parsed.title,
            set_code=outcome.parsed.set_code,
            card_number=outcome.parsed.card_number,
            year=outcome.parsed.year,
            hp=outcome.parsed.hp,
        ),
        ocr_provider=outcome.ocr.provider,
        ocr_confidence=outcome.ocr.mean_confidence,
        ocr_full_text=outcome.ocr.full_text,
        latency_ms=outcome.latency_ms,
        cost_usd=outcome.cost_usd,
    )


@router.post(
    "/identify/{identification_id}/feedback",
    status_code=204,
    summary="Submit thumbs-up / thumbs-down on a prior identification",
    dependencies=[Depends(feedback_limit)],
)
async def submit_identify_feedback(
    identification_id: uuid.UUID,
    body: IdentifyFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
) -> None:
    """Persist the user's correctness signal for future re-ranking."""
    if not body.correct and not body.chosen_card_id:
        # A "wrong" verdict without a correction is allowed but logged so
        # we can later prompt the user for the right card.
        logger.info(
            "identify-feedback negative w/o correction id=%s user=%s",
            identification_id,
            user.id,
        )
    identifier = CardIdentifier()
    try:
        await identifier.record_feedback(
            db,
            identification_id=identification_id,
            correct=body.correct,
            chosen_card_id=body.chosen_card_id,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return


@router.get(
    "/admin/ocr/metrics",
    response_model=IdentifyMetricsResponse,
    summary="OCR pipeline accuracy + cost metrics (admin)",
)
async def ocr_metrics(
    days: int = Query(30, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_user),
) -> IdentifyMetricsResponse:
    """Rolling accuracy/cost metrics over the last ``days`` days.

    .. note::
       TODO(admin-roles): there is no admin role on :class:`User` yet.
       For now any signed-in user can hit this endpoint; once roles
       land, gate with ``Depends(require_admin)``.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    # Pull every row in the window. The volume is bounded by our scan
    # rate-limits (~30/min/user) so we can comfortably aggregate in
    # Python rather than craft dialect-specific percentile SQL.
    rows = (
        await db.execute(
            select(
                CardIdentification.id,
                CardIdentification.ocr_provider,
                CardIdentification.tcg_inferred,
                CardIdentification.top_confidence,
                CardIdentification.latency_ms,
                CardIdentification.cost_usd,
            ).where(CardIdentification.created_at >= cutoff)
        )
    ).all()
    total = len(rows)
    by_provider: dict[str, int] = {}
    by_tcg: dict[str, int] = {}
    confidences: list[float] = []
    latencies: list[int] = []
    total_cost = 0.0
    for _id, provider, tcg_inferred, top_conf, latency_ms, cost in rows:
        by_provider[provider] = by_provider.get(provider, 0) + 1
        by_tcg[tcg_inferred] = by_tcg.get(tcg_inferred, 0) + 1
        confidences.append(float(top_conf or 0.0))
        latencies.append(int(latency_ms or 0))
        total_cost += float(cost or 0.0)

    feedback_rows = (
        (
            await db.execute(
                select(IdentificationFeedback.correct)
                .join(
                    CardIdentification,
                    CardIdentification.id == IdentificationFeedback.identification_id,
                )
                .where(CardIdentification.created_at >= cutoff)
            )
        )
        .scalars()
        .all()
    )
    total_feedback = len(feedback_rows)
    correct_feedback = sum(1 for c in feedback_rows if c)

    return IdentifyMetricsResponse(
        window_days=days,
        total_identifications=total,
        total_feedback=total_feedback,
        correct_feedback=correct_feedback,
        top1_accuracy=(correct_feedback / total_feedback) if total_feedback else 0.0,
        mean_confidence=(sum(confidences) / len(confidences)) if confidences else 0.0,
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        total_cost_usd=round(total_cost, 4),
        by_provider=by_provider,
        by_tcg=by_tcg,
    )


def _percentile(values: list[int], pct: int) -> int:
    """Pure-Python percentile (nearest-rank). Returns 0 for an empty list."""
    if not values:
        return 0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[k]
