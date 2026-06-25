"""Scanner-funnel analytics for the admin portal.

Reuses the OCR pipeline's :func:`compute_ocr_metrics` for accuracy/cost/latency
and layers on the pHash fast-path rate (the share resolved without paid OCR) and
scan-job lifecycle counts. Read-only.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identification import CardIdentification
from app.models.scan import ScanJob
from app.schemas.scanner_stats import ScannerStats
from app.services.identification.metrics_service import compute_ocr_metrics


async def summary(db: AsyncSession, *, days: int = 30) -> ScannerStats:
    m = await compute_ocr_metrics(db, days=days)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    sources = (
        await db.execute(
            select(CardIdentification.primary_source).where(
                CardIdentification.created_at >= cutoff
            )
        )
    ).scalars().all()
    by_source: dict[str, int] = dict(Counter(s or "none" for s in sources))
    total = len(sources)
    fast_path_rate = (by_source.get("phash", 0) / total) if total else 0.0

    scan_rows = (
        await db.execute(
            select(ScanJob.status).where(ScanJob.created_at >= cutoff)
        )
    ).scalars().all()
    scans_by_status: dict[str, int] = dict(
        Counter(s.value if hasattr(s, "value") else str(s) for s in scan_rows)
    )

    return ScannerStats(
        window_days=days,
        total_identifications=m.total_identifications,
        by_source=by_source,
        fast_path_rate=round(fast_path_rate, 4),
        total_feedback=m.total_feedback,
        correct_feedback=m.correct_feedback,
        top1_accuracy=round(m.top1_accuracy, 4),
        mean_confidence=round(m.mean_confidence, 4),
        latency_p50_ms=m.latency_p50_ms,
        latency_p95_ms=m.latency_p95_ms,
        total_cost_usd=m.total_cost_usd,
        by_provider=m.by_provider,
        by_tcg=m.by_tcg,
        scans_total=len(scan_rows),
        scans_by_status=scans_by_status,
    )


__all__ = ["summary"]
