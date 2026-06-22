"""Automatic monthly + yearly statement scheduler (Amex-style).

Users never tap "Generate". Statements materialise on their own at the
end of each cycle and live in the user's archive forever — exactly how
a credit-card statement works.

Cycle close rules
-----------------
* Monthly statements close at the start of the **following** month, UTC.
  Example: the *May 2026* statement becomes available shortly after
  ``2026-06-01 00:00 UTC``. We add a small cushion (run hourly, plus the
  scheduler's own boot latency) so cards added at 23:59 on the 31st are
  still captured before the period is rolled up.
* Yearly statements close at the start of the **following** year.
  Example: *2025* statement materialises shortly after ``2026-01-01``.

Eligibility rules (per user × period)
-------------------------------------
A statement is generated when **all** of the following hold:

1. The period has already closed (period_end < today, UTC).
2. The user existed on or before ``period_end`` (``users.created_at <=
   period_end``). New accounts don't get retroactive statements for
   months they weren't around for.
3. The user owned at least one (non-deleted) graded card on or before
   ``period_end``. Mirrors Amex's rule of not mailing $0 statements to
   accounts that have never been used.
4. There isn't already a row in :class:`UserReport` for
   ``(user, period, period_start)``.

Catch-up window
---------------
On every cycle we look back **12 months** for monthly and **3 years**
for yearly. Anything older is grandfathered as "the user wasn't using
the app yet". This bounds the worst-case workload after an outage and
keeps the boot scan fast.

Concurrency
-----------
Multiple Cloud Run instances are normal. To avoid two of them racing on
the same close cycle (and to keep load on the catalog providers down to
one pass) we wrap each cycle in a Postgres advisory lock. On SQLite or
any other dialect the lock is a no-op — local dev / tests still work,
they just don't have the cross-process guarantee.
"""

from __future__ import annotations

import asyncio
import calendar
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_sessionmaker
from app.models.enums import ReportPeriodEnum, ReportStatusEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.models.user_report import UserReport
from app.services.analytics.reports.service import generate_report
from app.utils.logger import get_logger

_log = get_logger("services.reports.scheduler")

# Stable 64-bit constant — derived from a fixed ASCII tag so it doesn't
# collide with any other advisory lock we might add later. Anyone else
# reaching for advisory locks should pick their own constant from a
# different region of the int space.
_ADVISORY_LOCK_KEY: int = 0x4C50_5253_4348_4544  # b"LPRSCHED"

# How far back we'll fill in missing statements.
MONTHLY_BACKFILL_MONTHS = 12
YEARLY_BACKFILL_YEARS = 3

# How long to sleep between cycle scans. 30 minutes means a monthly
# close that happens at 00:00 UTC is visible to users by 00:30 UTC in
# the worst case — well within "the next morning" expectations.
_SLEEP_BETWEEN_CYCLES = timedelta(minutes=30)


# ── period iteration ──────────────────────────────────────────────────


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Return ``(year, month)`` shifted by ``delta`` months (can be negative)."""
    total = year * 12 + (month - 1) + delta
    return total // 12, (total % 12) + 1


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Inclusive ``(start, end)`` UTC dates for the calendar month."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def iter_closed_monthly_periods(
    now: datetime, *, lookback_months: int = MONTHLY_BACKFILL_MONTHS
) -> Iterator[tuple[int, int]]:
    """Yield ``(year, month)`` for each closed month within the lookback.

    A month is "closed" once the calendar has rolled past its last day.
    The iterator yields newest-first so callers that bail early still
    cover the most recent (most user-visible) periods.
    """
    # Most-recent closed month is whichever one immediately precedes ``now``.
    cur_year, cur_month = _add_months(now.year, now.month, -1)
    for i in range(lookback_months):
        y, m = _add_months(cur_year, cur_month, -i)
        yield y, m


def iter_closed_yearly_periods(
    now: datetime, *, lookback_years: int = YEARLY_BACKFILL_YEARS
) -> Iterator[int]:
    """Yield each closed calendar year (newest-first) within the lookback."""
    for i in range(lookback_years):
        yield now.year - 1 - i


# ── eligibility ───────────────────────────────────────────────────────


async def _users_with_any_grade_on_or_before(
    db: AsyncSession, *, period_end: date
) -> list[User]:
    """Return every user that had ≥1 non-deleted graded card by ``period_end``.

    The cutoff is inclusive — a card created at any time on
    ``period_end`` still counts toward that period. We also exclude
    users created after the period (they didn't exist yet) at the join.
    """
    cutoff = datetime.combine(period_end, datetime.max.time(), tzinfo=UTC)
    stmt = (
        select(User)
        .where(User.created_at <= cutoff)
        .where(
            select(GradedCard.id)
            .where(GradedCard.user_id == User.id)
            .where(GradedCard.created_at <= cutoff)
            .where(GradedCard.deleted_at.is_(None))
            .exists()
        )
    )
    return list((await db.execute(stmt)).scalars())


async def _existing_report_keys(
    db: AsyncSession,
) -> set[tuple[str, str, date]]:
    """Return ``{(user_id, period.value, period_start)}`` already **ready**.

    We pull the whole set once per cycle (typically tiny) and use it to
    skip generation that would no-op anyway. Cheaper than asking the
    DB per user × period.

    Only ``ready`` rows count as "done". ``failed`` and stuck ``pending``
    rows are deliberately excluded so the next cycle re-attempts them —
    a statement that failed once (e.g. a transient storage/upload error)
    self-heals instead of showing "failed" forever. ``generate_report``
    reuses the existing row on retry, so this never mints duplicates.
    """
    rows = (
        await db.execute(
            select(
                UserReport.user_id, UserReport.period, UserReport.period_start
            ).where(UserReport.status == ReportStatusEnum.ready)
        )
    ).all()
    return {
        (str(uid), p.value if hasattr(p, "value") else str(p), ps)
        for uid, p, ps in rows
    }


# ── advisory lock ─────────────────────────────────────────────────────


@asynccontextmanager
async def _scheduler_lock(db: AsyncSession) -> AsyncIterator[bool]:
    """Try to take the scheduler-wide advisory lock.

    Yields ``True`` when this process holds the lock and should proceed
    with the cycle. Yields ``False`` when another instance already has
    it — the caller should just skip this cycle and try again later.

    On non-Postgres dialects (sqlite in tests) the lock is treated as
    always acquired; the single-process test runtime doesn't need it.
    """
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect != "postgresql":
        yield True
        return
    got = (
        await db.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
        )
    ).scalar_one()
    if not got:
        yield False
        return
    try:
        yield True
    finally:
        # Best-effort release; we don't fail the cycle if the unlock
        # call itself raises (the lock dies with the session anyway).
        try:
            await db.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
            )
        except Exception:
            pass


# ── one cycle ─────────────────────────────────────────────────────────


async def run_close_cycle(*, now: datetime | None = None) -> dict[str, int]:
    """Materialise every statement that should exist by ``now`` but doesn't.

    Returns a counter dict for logging / tests:
    ``{"checked": n, "generated": k, "skipped": s, "failed": f}``.
    """
    now = now or datetime.now(UTC)
    counts = {"checked": 0, "generated": 0, "skipped": 0, "failed": 0}
    sm = get_sessionmaker()

    async with sm() as db, _scheduler_lock(db) as acquired:
        if not acquired:
            _log.debug("scheduler lock held by another instance; skipping cycle")
            return counts

        existing = await _existing_report_keys(db)

        # Monthly sweep.
        for year, month in iter_closed_monthly_periods(now):
            period_start, period_end = _month_bounds(year, month)
            if period_end >= now.date():
                # Defensive — shouldn't happen given iter logic.
                continue
            eligible = await _users_with_any_grade_on_or_before(
                db, period_end=period_end
            )
            for user in eligible:
                counts["checked"] += 1
                key = (str(user.id), ReportPeriodEnum.monthly.value, period_start)
                if key in existing:
                    counts["skipped"] += 1
                    continue
                try:
                    await generate_report(
                        db,
                        user=user,
                        period=ReportPeriodEnum.monthly,
                        year=year,
                        month=month,
                    )
                    counts["generated"] += 1
                    existing.add(key)
                except Exception as exc:
                    counts["failed"] += 1
                    _log.warning(
                        "auto-generate monthly failed user=%s %04d-%02d: %s",
                        user.id,
                        year,
                        month,
                        exc,
                    )

        # Yearly sweep.
        for year in iter_closed_yearly_periods(now):
            period_start = date(year, 1, 1)
            period_end = date(year, 12, 31)
            if period_end >= now.date():
                continue
            eligible = await _users_with_any_grade_on_or_before(
                db, period_end=period_end
            )
            for user in eligible:
                counts["checked"] += 1
                key = (str(user.id), ReportPeriodEnum.yearly.value, period_start)
                if key in existing:
                    counts["skipped"] += 1
                    continue
                try:
                    await generate_report(
                        db,
                        user=user,
                        period=ReportPeriodEnum.yearly,
                        year=year,
                    )
                    counts["generated"] += 1
                    existing.add(key)
                except Exception as exc:
                    counts["failed"] += 1
                    _log.warning(
                        "auto-generate yearly failed user=%s %04d: %s",
                        user.id,
                        year,
                        exc,
                    )

    if counts["generated"] or counts["failed"]:
        _log.info(
            "reports scheduler cycle: checked=%d generated=%d skipped=%d failed=%d",
            counts["checked"],
            counts["generated"],
            counts["skipped"],
            counts["failed"],
        )
    return counts


# ── long-running loop ─────────────────────────────────────────────────


async def scheduler_loop() -> None:
    """Forever-running task that closes due statement cycles.

    Spawned from :mod:`app.lifecycle` at startup. Runs an initial pass
    immediately (so a service restart at 00:30 UTC on the 1st still
    closes yesterday's month) and then every ~30 minutes thereafter.
    """
    s = get_settings()
    if not getattr(s, "reports_scheduler_enabled", True):
        _log.info("reports scheduler disabled by settings; loop will not run")
        return
    if s.is_test:
        _log.info("reports scheduler disabled in test env")
        return

    _log.info("reports scheduler loop starting")
    while True:
        try:
            await run_close_cycle()
        except Exception:
            # ``run_close_cycle`` is internally defensive but we belt-and-
            # suspender here: the supervising _resilient_loop will see
            # any escape and restart us with backoff.
            _log.exception("reports scheduler cycle crashed")
            raise
        await asyncio.sleep(_SLEEP_BETWEEN_CYCLES.total_seconds())


# ── next-close helpers (consumed by the API + UI) ─────────────────────


def next_monthly_close(now: datetime | None = None) -> datetime:
    """Datetime when the *current* month's statement will become available.

    For ``now = 2026-05-21`` returns ``2026-06-01 00:00:00 UTC``.
    """
    now = now or datetime.now(UTC)
    year, month = _add_months(now.year, now.month, 1)
    return datetime(year, month, 1, tzinfo=UTC)


def next_yearly_close(now: datetime | None = None) -> datetime:
    """Datetime when the *current* year's statement will become available."""
    now = now or datetime.now(UTC)
    return datetime(now.year + 1, 1, 1, tzinfo=UTC)


__all__ = [
    "MONTHLY_BACKFILL_MONTHS",
    "YEARLY_BACKFILL_YEARS",
    "iter_closed_monthly_periods",
    "iter_closed_yearly_periods",
    "next_monthly_close",
    "next_yearly_close",
    "run_close_cycle",
    "scheduler_loop",
]
