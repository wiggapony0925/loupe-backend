"""Scanner-funnel analytics for the admin portal.

Reuses the OCR pipeline's :func:`compute_ocr_metrics` for accuracy/cost/latency
and layers on the pHash fast-path rate (the share resolved without paid OCR) and
scan-job lifecycle counts. Read-only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identification import CardIdentification
from app.models.scan import ScanJob
from app.schemas.scanner_stats import ScannerStats, ScannerTrend, ScannerTrendPoint
from app.services.identification.metrics_service import compute_ocr_metrics


def _percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    k = min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1)))
    return int(ordered[k])


async def summary(db: AsyncSession, *, days: int = 30) -> ScannerStats:
    m = await compute_ocr_metrics(db, days=days)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    sources = (
        (
            await db.execute(
                select(CardIdentification.primary_source).where(
                    CardIdentification.created_at >= cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    by_source: dict[str, int] = dict(Counter(s or "none" for s in sources))
    total = len(sources)
    fast_path_rate = (by_source.get("phash", 0) / total) if total else 0.0

    scan_rows = (
        (await db.execute(select(ScanJob.status).where(ScanJob.created_at >= cutoff)))
        .scalars()
        .all()
    )
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


async def trend(db: AsyncSession, *, days: int = 30) -> ScannerTrend:
    """Per-day speed + accuracy series for the scanner trend charts.

    Buckets ``CardIdentification`` rows by UTC day (in Python, so it's portable
    across Postgres/SQLite) and fills every day in the window so the chart has a
    continuous x-axis. Days with no scans report zeros — the frontend renders
    those as gaps in the accuracy/latency lines, not dips to zero.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                CardIdentification.created_at,
                CardIdentification.top_confidence,
                CardIdentification.latency_ms,
                CardIdentification.primary_source,
            ).where(CardIdentification.created_at >= cutoff)
        )
    ).all()

    buckets: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    for created_at, conf, latency, source in rows:
        day = created_at.date().isoformat()
        buckets[day].append((float(conf or 0.0), int(latency or 0), source or "none"))

    start = (datetime.now(UTC) - timedelta(days=days)).date()
    points: list[ScannerTrendPoint] = []
    for i in range(days + 1):
        day = (start + timedelta(days=i)).isoformat()
        items = buckets.get(day, [])
        if items:
            confs = [c for c, _, _ in items]
            lats = [ms for _, ms, _ in items]
            fast = sum(1 for _, _, s in items if s == "phash")
            points.append(
                ScannerTrendPoint(
                    date=day,
                    count=len(items),
                    mean_confidence=round(sum(confs) / len(confs), 4),
                    latency_p50_ms=_percentile(lats, 50),
                    latency_p95_ms=_percentile(lats, 95),
                    fast_path_rate=round(fast / len(items), 4),
                )
            )
        else:
            points.append(
                ScannerTrendPoint(
                    date=day,
                    count=0,
                    mean_confidence=0.0,
                    latency_p50_ms=0,
                    latency_p95_ms=0,
                    fast_path_rate=0.0,
                )
            )
    return ScannerTrend(window_days=days, points=points)


__all__ = ["summary", "trend"]
