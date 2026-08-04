"""Scheduled lifecycle email: the weekly portfolio digest and Pro-expiry notices.

Both jobs are *reporting* jobs — they read state the rest of the platform
already maintains (portfolio snapshots, Stripe subscription status) and mail a
summary. Neither mutates domain data, so a failed run is safe to retry and a
skipped run costs at most one late email.

Delivery is queued in the background by ``email_service``, so both jobs call
``drain()`` before returning: a one-shot worker process must not exit with
mail still in flight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.user import User, UserSettings
from app.services import email_service
from app.services.analytics import home_feed_service
from app.services.auth import unsubscribe_service
from app.utils.logger import get_logger

logger = get_logger("workers.lifecycle_email")

#: Digest look-back. Also the minimum spacing between digests.
DIGEST_WINDOW_DAYS = 7

#: Don't mail a "digest" to someone with nothing to report — an empty vault
#: or a single snapshot has no move to describe, and a $0 email is churn.
MIN_SNAPSHOTS = 2

#: Safety cap per run: a runaway user table shouldn't turn one cron tick into
#: an unbounded fan-out. Anything above this is a signal to shard the job.
MAX_RECIPIENTS_PER_RUN = 5_000

#: Movers shown in the digest body.
DIGEST_MOVERS = 3

#: Days before expiry that earn a heads-up. A user who cancels gets one notice
#: a few days out and a last one the day before — not a daily countdown.
EXPIRY_NOTICE_DAYS = (7, 1)


async def _digest_subscribers(db: AsyncSession) -> list[User]:
    """Active users who haven't opted out of non-transactional mail.

    Same opt-out semantics as announcements (``email_announcements_enabled``,
    defaulting to subscribed when no settings row exists) — the digest is
    recurring product mail, so the unsubscribe must govern it too.
    """
    rows = (
        await db.execute(
            select(User)
            .outerjoin(UserSettings, UserSettings.user_id == User.id)
            .where(
                User.deleted_at.is_(None),
                User.banned_at.is_(None),
                User.email.is_not(None),
                (UserSettings.user_id.is_(None))
                | (UserSettings.email_announcements_enabled.is_(True)),
            )
            .limit(MAX_RECIPIENTS_PER_RUN)
        )
    ).scalars()
    return list(rows)


async def _digest_for(
    db: AsyncSession, user: User, *, since: datetime
) -> email_service.DigestRecipient | None:
    """Build one user's digest slice, or None when there's nothing to say."""
    snaps = (
        (
            await db.execute(
                select(PortfolioSnapshot)
                # NULL collection = the whole-vault series. Mixing scoped rows
                # in here would report a single collection as the portfolio.
                .where(
                    PortfolioSnapshot.user_id == user.id,
                    PortfolioSnapshot.collection_id.is_(None),
                    PortfolioSnapshot.captured_at >= since,
                )
                .order_by(PortfolioSnapshot.captured_at)
            )
        )
        .scalars()
        .all()
    )
    if len(snaps) < MIN_SNAPSHOTS:
        return None
    first, last = snaps[0], snaps[-1]
    if not last.total_value_usd or last.holdings_count <= 0:
        return None
    start = Decimal(first.total_value_usd)
    end = Decimal(last.total_value_usd)
    delta_usd = end - start
    # A vault that started the window at zero has no meaningful percentage.
    delta_pct = float(delta_usd / start * 100) if start > 0 else 0.0

    movers: list[tuple[str, float]] = []
    try:
        for row in await home_feed_service.top_movers(db, user, limit=DIGEST_MOVERS):
            name, pct = row.get("cardName"), row.get("changePct1y")
            if name and pct is not None:
                movers.append((name, float(pct)))
    except Exception as exc:  # movers are a bonus, never a reason to skip
        logger.warning("digest movers failed for user=%s (%s)", user.id, exc)

    return email_service.DigestRecipient(
        user=user,
        unsub_url=unsubscribe_service.unsubscribe_url(str(user.id)),
        total_value_usd=end,
        delta_pct=delta_pct,
        delta_usd=delta_usd,
        card_count=last.holdings_count,
        series=[float(s.total_value_usd) for s in snaps],
        top_movers=movers or None,
    )


async def send_portfolio_digests(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Weekly "here's what your collection did" mail.

    Skips anyone without at least two whole-vault snapshots in the window —
    new accounts and dormant vaults get silence rather than a $0.00 email.
    """
    since = datetime.now(UTC) - timedelta(days=DIGEST_WINDOW_DAYS)
    recipients: list[email_service.DigestRecipient] = []
    async with get_sessionmaker()() as session:
        for user in await _digest_subscribers(session):
            slice_ = await _digest_for(session, user, since=since)
            if slice_ is not None:
                recipients.append(slice_)
    sent = 0
    if recipients:
        sent = await email_service.send_portfolio_digest(
            recipients, period_label="This week"
        )
    await email_service.drain()
    logger.info("portfolio digest: %d eligible, %d accepted", len(recipients), sent)
    return {"eligible": len(recipients), "sent": sent}


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _is_ending(user: User) -> bool:
    """True only when the subscription is genuinely set to stop.

    ``pro_expires_at`` is the period end for *every* active subscription,
    auto-renewing ones included — mailing "your Pro ends in 3 days" to someone
    who is about to be billed again would be alarming and wrong. Stripe holds
    the only copy of ``cancel_at_period_end``, so ask it. The candidate set is
    tiny (one or two days' worth of expiries), so the extra calls are cheap.

    Fails closed: if Stripe can't be reached, send nothing.
    """
    from app.services import billing_service

    if not billing_service.billing_configured() or not user.stripe_subscription_id:
        return False
    try:
        import stripe

        billing_service._client()
        sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
        return bool(sub.get("cancel_at_period_end")) or sub.get("status") in {
            "canceled",
            "unpaid",
        }
    except Exception as exc:
        logger.warning("expiry check failed for user=%s (%s)", user.id, exc)
        return False


async def send_pro_expiry_notices(ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """Daily heads-up for Pro memberships that are actually about to end."""
    now = datetime.now(UTC)
    # +1 day of slack: `days_left` truncates, so a subscription 7d02h out has
    # days_left == 7 and must still be in the candidate set. Without the extra
    # day the query would exclude it, and by the next run it reads as 6 — a
    # milestone that isn't in the list, so the 7-day notice would never fire.
    horizon = now + timedelta(days=max(EXPIRY_NOTICE_DAYS) + 1)
    sent = 0
    checked = 0
    async with get_sessionmaker()() as session:
        candidates = (
            (
                await session.execute(
                    select(User).where(
                        User.plan == "pro",
                        User.deleted_at.is_(None),
                        User.banned_at.is_(None),
                        User.email.is_not(None),
                        User.pro_expires_at.is_not(None),
                        User.pro_expires_at <= horizon,
                        User.pro_expires_at > now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for user in candidates:
            ends = _as_aware(user.pro_expires_at)
            if ends is None:
                continue
            days_left = (ends - now).days
            # Fire on the notice days only, so a 7-day window doesn't become
            # seven emails. `days` truncates, so day 6.4 reads as 6 and is skipped.
            if days_left not in EXPIRY_NOTICE_DAYS:
                continue
            checked += 1
            if not await _is_ending(user):
                continue
            ok = await email_service.send_pro_expiring(
                user,
                ends_on=ends,
                days_left=days_left,
                # One notice per user per milestone, even if the job re-runs.
                idempotency_key=f"pro-expiring-{user.id}-{ends.date()}-{days_left}",
            )
            sent += int(bool(ok))
    await email_service.drain()
    logger.info("pro expiry notices: %d at a milestone, %d sent", checked, sent)
    return {"checked": checked, "sent": sent}


__all__ = ["send_portfolio_digests", "send_pro_expiry_notices"]
