"""User-facing report-generation orchestrator.

Owns the full lifecycle of a :class:`~app.models.user_report.UserReport`:

1. Resolves the requested period (monthly / yearly) into UTC date bounds.
2. Inserts (or reuses) the `pending` row inside a savepoint so concurrent
   "Generate" presses don't race past the `uq_user_reports_user_period_start`
   unique constraint.
3. Builds the snapshot, renders the PDF, uploads it to object storage.
4. Flips the row to `ready` (or `failed` with a captured error message).

Generation is intentionally synchronous from the router's perspective —
even a year-long statement for a 1000-card vault renders in well under
the API timeout. Larger workloads can be moved to the arq worker later
without changing the public surface (the lifecycle is already tolerant
of out-of-band updates).
"""

from __future__ import annotations

import calendar
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ReportPeriodEnum, ReportStatusEnum
from app.models.user import User
from app.models.user_report import UserReport
from app.services.analytics.reports.aggregator import build_snapshot
from app.services.analytics.reports.images import hydrate_report_images
from app.services.analytics.reports.pdf_builder import render_pdf
from app.services.analytics.reports.storage import upload_report_pdf
from app.utils.logger import get_logger

_log = get_logger("services.reports")


def resolve_period(
    period: ReportPeriodEnum, *, year: int, month: int | None
) -> tuple[date, date, str]:
    """Convert (period, year, month?) into inclusive (start, end, label)."""
    today = datetime.now(UTC).date()
    if period is ReportPeriodEnum.monthly:
        if month is None:
            raise ValueError("month is required for monthly reports")
        if not (1 <= month <= 12):
            raise ValueError("month must be in 1..12")
        last_day = calendar.monthrange(year, month)[1]
        start = date(year, month, 1)
        end = date(year, month, last_day)
        label = f"{calendar.month_name[month]} {year}"
    else:
        start = date(year, 1, 1)
        end = date(year, 12, 31)
        label = f"{year}"
    # Refuse future windows — we don't fabricate forecasts.
    if start > today:
        raise ValueError("Cannot generate a report for a period that has not started")
    # Clamp end to today so a request for the current month doesn't
    # claim to cover days that haven't happened yet.
    if end > today:
        end = today
    return start, end, label


async def _get_or_create_pending(
    db: AsyncSession,
    *,
    user: User,
    period: ReportPeriodEnum,
    period_start: date,
    period_end: date,
    title: str,
    collection_id: uuid.UUID | None = None,
    collection_name: str | None = None,
) -> tuple[UserReport, bool]:
    """Return ``(row, created)``. Reuses an existing row for the same window.

    A row is "reused" when it's pending or failed — we treat the new
    request as a retry. A `ready` row is returned as-is; the router
    short-circuits and the caller can simply download it. Scoped and
    whole-vault statements for the same window are distinct rows
    (``collection_id`` participates in the match — ``== None`` compiles
    to ``IS NULL``).
    """
    existing = (
        await db.execute(
            select(UserReport).where(
                UserReport.user_id == user.id,
                UserReport.period == period,
                UserReport.period_start == period_start,
                UserReport.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status is ReportStatusEnum.ready:
            return existing, False
        existing.status = ReportStatusEnum.pending
        existing.error_message = None
        existing.title = title
        existing.collection_name = collection_name
        await db.flush()
        return existing, False

    row = UserReport(
        user_id=user.id,
        period=period,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatusEnum.pending,
        title=title,
        collection_id=collection_id,
        collection_name=collection_name,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent request beat us to it — re-fetch the winner.
        await db.rollback()
        winner = (
            await db.execute(
                select(UserReport).where(
                    UserReport.user_id == user.id,
                    UserReport.period == period,
                    UserReport.period_start == period_start,
                    UserReport.collection_id == collection_id,
                )
            )
        ).scalar_one()
        return winner, False
    return row, True


async def generate_report(
    db: AsyncSession,
    *,
    user: User,
    period: ReportPeriodEnum,
    year: int,
    month: int | None = None,
    collection_id: uuid.UUID | None = None,
    collection_name: str | None = None,
) -> UserReport:
    """Render + persist a portfolio statement for *(user, period, year[, month])*.

    Passing ``collection_id`` scopes the statement to one collection —
    the PDF then covers only holdings in that portfolio and its title
    carries the collection name so the archive stays unambiguous.
    """
    period_start, period_end, label = resolve_period(period, year=year, month=month)
    scoped_label = f"{label} · {collection_name}" if collection_name else label
    title = f"{scoped_label} statement"[:120]
    row, _created = await _get_or_create_pending(
        db,
        user=user,
        period=period,
        period_start=period_start,
        period_end=period_end,
        title=title,
        collection_id=collection_id,
        collection_name=collection_name,
    )
    # Already rendered — nothing to do, hand the row back.
    if row.status is ReportStatusEnum.ready and row.storage_key:
        return row

    await db.commit()
    await db.refresh(row)

    # Track the stage we're in so a failure surfaces *where* it broke
    # (snapshot / render / upload) rather than a bare, context-free
    # exception string. This is what the web Statements page shows.
    stage = "snapshot"
    try:
        snap = await build_snapshot(
            db,
            user=user,
            period_label=scoped_label,
            period_start=period_start,
            period_end=period_end,
            collection_id=collection_id,
            collection_name=collection_name,
        )
        stage = "render"
        # Best-effort card art for the movers tables + top-holdings gallery.
        # Internal try/except: a CDN hiccup can never fail the statement.
        await hydrate_report_images(snap)
        pdf_bytes = render_pdf(snap)
        stage = "upload"
        storage_key = await upload_report_pdf(user.id, row.id, pdf_bytes)
        row.storage_key = storage_key
        row.file_size_bytes = len(pdf_bytes)
        row.status = ReportStatusEnum.ready
        row.generated_at = datetime.now(UTC)
        row.error_message = None
    except Exception as exc:
        _log.exception(
            "report generation failed for user=%s period=%s at stage=%s",
            user.id,
            label,
            stage,
        )
        row.status = ReportStatusEnum.failed
        row.error_message = f"{stage}: {exc}"[:500]
    await db.commit()
    await db.refresh(row)
    # "Your statement is ready" — the scheduler generates these while the
    # user is away, so email is how they learn the period closed (Amex
    # mails theirs too). After commit, best-effort; the idempotency key
    # stops a same-day regeneration from double-mailing.
    if row.status is ReportStatusEnum.ready:
        from app.services import email_service

        # Real numbers from the snapshot power the chart + stat tiles in the
        # email. getattr-guarded: tests stub the snapshot with a bare dict.
        series_points = getattr(snap, "series", None) or []
        series = [float(p.value_usd) for p in series_points][-24:]
        await email_service.send_statement_ready(
            user,
            title=title,
            idempotency_key=f"statement-ready-{row.id}",
            total_value_usd=getattr(snap, "closing_value_usd", None),
            delta_pct=getattr(snap, "delta_pct", None),
            card_count=getattr(snap, "card_count", None),
            series=series if len(series) >= 2 else None,
        )
    return row


async def list_reports(db: AsyncSession, user: User) -> list[UserReport]:
    rows = (
        await db.execute(
            select(UserReport)
            .where(UserReport.user_id == user.id)
            .order_by(UserReport.period_start.desc(), UserReport.created_at.desc())
        )
    ).scalars()
    return list(rows)


async def get_report(
    db: AsyncSession, user: User, report_id: uuid.UUID
) -> UserReport | None:
    return (
        await db.execute(
            select(UserReport).where(
                UserReport.id == report_id, UserReport.user_id == user.id
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "generate_report",
    "get_report",
    "list_reports",
    "resolve_period",
]
