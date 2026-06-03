"""``/cards/identify*`` endpoints — image → ranked card candidates.

Three public surfaces:

* ``POST /v1/cards/identify`` — multipart image upload, returns the top-N
  candidates plus an ``identification_id`` the client uses for feedback.
  Auth is *optional* (matches the public ``GET /cards/search`` posture)
  so the camera-first onboarding flow works pre-login.
* ``POST /v1/cards/identify/{id}/feedback`` — thumbs-up/down + optional
  correction. Auth required (we want to attribute the signal).
* ``GET /v1/cards/admin/ocr/metrics`` — rolling accuracy + cost
  dashboard. Admin-only: caller must be in the ``ADMIN_EMAILS``
  allowlist (see :class:`Settings`).
"""

from __future__ import annotations

import dataclasses
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import optional_user, require_admin, require_user
from app.config import get_settings
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import rate_limit
from app.schemas.identification import (
    IdentifyByTextRequest,
    IdentifyCandidate,
    IdentifyFeedbackRequest,
    IdentifyMetricsResponse,
    IdentifyParsed,
    IdentifyResponse,
)
from app.services.identification.card_identifier import CardIdentifier
from app.services.identification.metrics_service import compute_ocr_metrics
from app.utils.logger import get_logger

logger = get_logger("routers.cards.identify")

router = APIRouter(prefix="/cards", tags=["cards"])

# Tight on the identify endpoint — Vision calls are expensive and we
# rate-limit per-user / per-IP rather than per-route globally. The live
# scanner polls ~1 frame/sec while open, so 30/min got blown out within
# seconds (a burst of 429s mid-scan corrupted the result stream). 60/min
# matches the other fan-out endpoints and keeps a continuous scan under
# the cap; absolute Vision spend is still guarded by the monthly budget
# check inside the identifier.
identify_limit = rate_limit(limit=60, window_seconds=60, name="cards.identify")
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

    top = outcome.candidates[0] if outcome.candidates else None
    logger.info(
        "identify summary id=%s user=%s hint=%s inferred=%s provider=%s "
        "latency_ms=%s ocr_conf=%.3f parsed_title=%r parsed_number=%r "
        "candidates=%s top=%r top_conf=%.3f source=%s fallback=%s",
        outcome.identification_id,
        user.id if user else None,
        tcg,
        outcome.tcg_inferred,
        outcome.ocr.provider,
        outcome.latency_ms,
        outcome.ocr.mean_confidence,
        outcome.parsed.title,
        outcome.parsed.card_number,
        len(outcome.candidates),
        top.name if top else None,
        top.confidence if top else 0.0,
        top.source if top else "none",
        outcome.fallback_required,
    )

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
        fallback_required=outcome.fallback_required,
        fallback_reason=outcome.fallback_reason,
    )


@router.post(
    "/identify/text",
    response_model=IdentifyResponse,
    summary="Identify a card from client-side OCR text (budget fallback)",
    dependencies=[Depends(identify_limit)],
)
async def identify_card_from_text(
    body: IdentifyByTextRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(optional_user),
) -> IdentifyResponse:
    """Run the catalog / scoring pipeline against text the client OCR'd.

    Used when ``POST /v1/cards/identify`` returned ``fallback_required=true``
    (typically because the monthly Vision budget is exhausted). The
    client extracts text on-device with Apple Vision or ML Kit and
    POSTs it here. No paid OCR call is made; ``cost_usd`` is always 0.
    """
    identifier = CardIdentifier()
    outcome = await identifier.identify_from_text(
        db,
        ocr_text=body.text,
        tcg_hint=body.tcg,
        client_provider=body.client_provider,
        ocr_confidence=body.ocr_confidence,
        user_id=user.id if user else None,
    )
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
        fallback_required=False,
        fallback_reason=None,
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
    _admin: User = Depends(require_admin),
) -> IdentifyMetricsResponse:
    """Rolling accuracy/cost metrics over the last ``days`` days.

    Gated by :func:`require_admin` — caller's email must be in the
    ``ADMIN_EMAILS`` allowlist. Returns 403 otherwise.
    """
    return await compute_ocr_metrics(db, days=days)
