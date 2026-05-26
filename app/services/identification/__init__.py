"""End-to-end card identification pipeline.

Given an image of a trading card, :class:`CardIdentifier` returns a
ranked list of catalog candidates with a confidence score, persists the
attempt for analytics, and exposes a feedback hook so user corrections
can boost future ranking on similar OCR text.

The pipeline is intentionally a thin orchestrator over small, testable
helpers (``image_ops``, ``text_parser``, ``tcg_infer``, ``confidence``)
so each step can be replaced or evaluated in isolation by the
``scripts/ocr_eval.py`` harness.
"""

from app.services.identification.card_identifier import (
    CandidateOut,
    CardIdentifier,
    IdentifyOutcome,
)

__all__ = ["CandidateOut", "CardIdentifier", "IdentifyOutcome"]
