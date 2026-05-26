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
    fallback_required: bool = Field(
        default=False,
        description=(
            "When true, the server refused to call the paid OCR provider "
            "(typically because OCR_MONTHLY_BUDGET_USD is exhausted). "
            "The client should run on-device OCR (Apple Vision / ML Kit) "
            "and resubmit via POST /v1/cards/identify/text."
        ),
    )
    fallback_reason: str | None = Field(
        default=None,
        description="Human-readable reason when fallback_required is true.",
    )


class IdentifyByTextRequest(BaseModel):
    """``POST /v1/cards/identify/text`` body — client-side OCR fallback."""

    text: str = Field(
        min_length=1,
        max_length=8000,
        description="Raw OCR text the client extracted on-device.",
    )
    tcg: str | None = Field(
        default=None,
        description='Optional hint: "pokemon" | "magic" | "yugioh" | …',
    )
    client_provider: str = Field(
        default="client_fallback",
        max_length=40,
        description='Identifier for the on-device engine, e.g. "apple_vision" / "mlkit".',
    )
    ocr_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Mean confidence reported by the on-device OCR.",
    )


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
    "IdentifyByTextRequest",
    "IdentifyCandidate",
    "IdentifyFeedbackRequest",
    "IdentifyMetricsResponse",
    "IdentifyParsed",
    "IdentifyResponse",
]
