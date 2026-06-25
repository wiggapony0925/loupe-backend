"""Schemas for the admin scanner-funnel surface.

Rolling identification metrics — accuracy, the pHash fast-path rate (skips paid
OCR), latency, spend — plus scan-job outcomes. Built on the existing OCR metrics
aggregation; this just adds the source breakdown and scan-job lifecycle counts.
"""

from __future__ import annotations

from pydantic import BaseModel


class ScannerStats(BaseModel):
    window_days: int
    total_identifications: int

    # Which signal won the match: "phash" (free fast path) | "text" (OCR) | ...
    by_source: dict[str, int]
    fast_path_rate: float  # share resolved by pHash without paid OCR

    # Accuracy (from user/feedback confirmations).
    total_feedback: int
    correct_feedback: int
    top1_accuracy: float
    mean_confidence: float

    # Performance + spend.
    latency_p50_ms: int
    latency_p95_ms: int
    total_cost_usd: float

    by_provider: dict[str, int]
    by_tcg: dict[str, int]

    # Scan-job lifecycle (hardware + phone uploads).
    scans_total: int
    scans_by_status: dict[str, int]


__all__ = ["ScannerStats"]
