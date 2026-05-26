"""Pydantic schemas for the card-identification API.

These shape the JSON returned by ``POST /v1/cards/identify`` and consumed
by ``POST /v1/cards/identify/{id}/feedback``. The pipeline service returns
its own dataclasses (``IdentifyOutcome`` / ``CandidateOut``); the router
converts to these models before responding so we don't leak internal
fields.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class IdentifyCandidate(BaseModel):
    """One ranked candidate in :class:`IdentifyResponse.candidates`."""

    model_config = ConfigDict(from_attributes=True)

    card_id: str | None = Field(
        default=None,
        description="Local catalog UUID (string) when the card has been materialized.",
    )
    upstream_id: str | None = Field(
        default=None,
        description='Composite "<source>:<external_id>" id (e.g. "pokemontcg:base1-4").',
    )
    name: str
    set_name: str | None = None
    set_code: str | None = None
    number: str | None = None
    image_url: str | None = None
    tcg: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(description='Which signal won: "text" | "phash" | "feedback".')
    breakdown: dict[str, float] = Field(
        default_factory=dict,
        description="Per-signal weights that produced ``confidence``.",
    )


class IdentifyParsed(BaseModel):
    """Structured fields extracted from OCR text. All optional."""

    title: str | None = None
    set_code: str | None = None
    card_number: str | None = None
    year: int | None = None
    hp: int | None = None


class IdentifyResponse(BaseModel):
    """``POST /v1/cards/identify`` response body (pre-envelope)."""

    identification_id: uuid.UUID
    candidates: list[IdentifyCandidate]
    accuracy_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence of the top candidate, 0 if none were found.",
    )
    primary_source: str
    tcg_inferred: str
    parsed: IdentifyParsed
    ocr_provider: str
    ocr_confidence: float = Field(ge=0.0, le=1.0)
    ocr_full_text: str
    latency_ms: int
    cost_usd: float


class IdentifyFeedbackRequest(BaseModel):
    """``POST /v1/cards/identify/{id}/feedback`` body."""

    correct: bool = Field(description="True = the top candidate matched the real card.")
    chosen_card_id: str | None = Field(
        default=None,
        description=(
            "Optional correction. May be a local UUID or composite upstream_id "
            "string. Required when ``correct`` is False to drive future re-ranks."
        ),
        max_length=64,
    )


# ────────────────────────────────────────────────────────── admin metrics


class IdentifyMetricsResponse(BaseModel):
    """``GET /v1/cards/admin/ocr/metrics`` body."""

    window_days: int
    total_identifications: int
    total_feedback: int
    correct_feedback: int
    top1_accuracy: float = Field(ge=0.0, le=1.0)
    mean_confidence: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: int
    latency_p95_ms: int
    total_cost_usd: float
    by_provider: dict[str, int]
    by_tcg: dict[str, int]


__all__ = [
    "IdentifyCandidate",
    "IdentifyFeedbackRequest",
    "IdentifyMetricsResponse",
    "IdentifyParsed",
    "IdentifyResponse",
]
