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


class ScannerTrendPoint(BaseModel):
    """One day of identify activity for the speed + accuracy trend charts."""

    date: str  # YYYY-MM-DD (UTC)
    count: int
    mean_confidence: float  # 0-1, avg top-1 confidence that day
    latency_p50_ms: int
    latency_p95_ms: int
    fast_path_rate: float  # share resolved by pHash (free, instant) that day


class ScannerTrend(BaseModel):
    window_days: int
    points: list[ScannerTrendPoint]


# ── Scan history log ────────────────────────────────────────────────────
# One row per identify call, with the scanned frame + who/when/what-we-said.


class ScanHistoryCandidate(BaseModel):
    """One ranked candidate the scanner returned for a scan."""

    upstream_id: str | None = None
    card_id: str | None = None
    name: str
    confidence: float
    source: str  # "text" | "phash" | "feedback"


class ScanHistoryItem(BaseModel):
    """A single scan for the admin history grid."""

    id: str
    created_at: str  # ISO-8601 UTC

    # Who scanned. Null = anonymous (pre-login camera-first flow).
    user_id: str | None = None
    user_email: str | None = None

    # The frame the user scanned, as a ready-to-render data URL (or null when
    # no thumbnail was captured — e.g. the on-device text fallback path or a
    # pre-migration row).
    image_url: str | None = None

    # What the scanner said.
    top_name: str | None = None
    top_upstream_id: str | None = None
    top_confidence: float
    primary_source: str  # "phash" | "text" | "none" | ...
    candidate_count: int

    # How it got there.
    tcg_inferred: str
    ocr_provider: str
    parsed_title: str | None = None
    parsed_number: str | None = None
    latency_ms: int
    cost_usd: float

    # Whether the user later confirmed / corrected this scan (if any).
    feedback_correct: bool | None = None


class ScanHistoryPage(BaseModel):
    """A cursor page of scan-history rows (newest first)."""

    items: list[ScanHistoryItem]
    # Opaque cursor for the next (older) page; null when there are no more.
    next_cursor: str | None = None
    total: int


class ScanHistoryDetail(ScanHistoryItem):
    """Full drill-down for one scan: every candidate + the raw OCR text."""

    ocr_full_text: str | None = None
    ocr_confidence: float
    parsed_set_code: str | None = None
    phash: str | None = None
    image_sha256: str | None = None
    candidates: list[ScanHistoryCandidate]


__all__ = [
    "ScanHistoryCandidate",
    "ScanHistoryDetail",
    "ScanHistoryItem",
    "ScanHistoryPage",
    "ScannerStats",
    "ScannerTrend",
    "ScannerTrendPoint",
]
