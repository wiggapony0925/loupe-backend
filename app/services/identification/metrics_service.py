"""OCR pipeline metrics aggregation.

Extracted from the ``GET /cards/admin/ocr/metrics`` endpoint so the
router stays a thin HTTP shell. Volumes are bounded by per-user scan
rate limits (~30/min), which keeps the in-Python aggregation cheap and
lets us avoid dialect-specific percentile SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identification import CardIdentification, IdentificationFeedback
from app.schemas.identification import IdentifyMetricsResponse


def _percentile(values: list[int], pct: int) -> int:
    """Pure-Python percentile (nearest-rank). Returns 0 for an empty list."""
    if not values:
        return 0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[k]


async def compute_ocr_metrics(
    db: AsyncSession, *, days: int
) -> IdentifyMetricsResponse:
    """Return rolling OCR accuracy + cost metrics over the last ``days`` days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)

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


__all__ = ["compute_ocr_metrics"]
