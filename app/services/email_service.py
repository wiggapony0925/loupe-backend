"""Transactional email — delivery, background dispatch, and the send_* API.

Templates live in :mod:`app.services.email_templates` (see its README) as
pure ``build_*`` functions; this module owns everything with side effects:

- ``send_email`` / ``send_batch`` — the Resend transport (best-effort,
  never raises, one retry on transient failures).
- ``dispatch`` / ``drain`` — a tiny background queue. ``send_*`` wrappers
  render the message *synchronously* (so ORM objects are still attached)
  and hand only the finished payload to a background task — an API call
  that triggers email (register, webhook, admin action) never waits on the
  provider. ``drain()`` runs at app shutdown and at the end of worker jobs
  so queued mail isn't lost.
- ``send_*`` wrappers — one per template; all no-ops (logged) until
  ``RESEND_API_KEY`` + ``NOTIFICATIONS_FROM_EMAIL`` are set, so callers can
  fire them unconditionally.

Deliverability practices are baked into the renderer (plain-text part,
preheader, escaped user content) and into this transport (category tags,
one-click ``List-Unsubscribe`` on announcement-class mail, idempotency keys
on webhook-triggered sends).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, NamedTuple

import httpx

from app.config import get_settings
from app.models.user import User
from app.services import email_log_service

# Re-export the whole template surface: callers (and tests, and the admin
# preview gallery) reach builders through this module.
from app.services.email_templates import *  # noqa: F403
from app.services.email_templates import (
    EmailContent,
    build_account_locked,
    build_admin_granted,
    build_ban_notice,
    build_blog_announcement,
    build_custom_announcement,
    build_free_limit_reached,
    build_mfa_disabled,
    build_mfa_enabled,
    build_new_sign_in,
    build_password_changed,
    build_password_reset,
    build_payment_failed,
    build_portfolio_digest,
    build_price_alert,
    build_pro_activated,
    build_pro_canceled,
    build_pro_expiring,
    build_reset_unavailable,
    build_set_completed,
    build_statement_ready,
    build_support_message,
    build_verify_email,
    build_waitlist_confirmation,
    build_waitlist_invite,
    build_welcome,
    render_email,
)
from app.utils.logger import get_logger

logger = get_logger("email")

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_RESEND_BATCH_ENDPOINT = "https://api.resend.com/emails/batch"
_BATCH_LIMIT = 100  # Resend max messages per /emails/batch call

# Transient provider responses worth one retry (rate limit / server hiccup).
_RETRYABLE = {429, 500, 502, 503, 504}
_RETRY_DELAY_SEC = 1.5


# ── Background dispatch ───────────────────────────────────────────────────

_PENDING: set[asyncio.Task[Any]] = set()


def dispatch(coro: Coroutine[Any, Any, Any]) -> None:
    """Run ``coro`` in the background (fire-and-forget, failures logged).

    Requires a running event loop — true everywhere we send email (request
    handlers, webhooks, worker jobs). Tasks are tracked so :func:`drain`
    can flush them at shutdown / end-of-job.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _PENDING.add(task)
    task.add_done_callback(_on_done)


def _on_done(task: asyncio.Task[Any]) -> None:
    _PENDING.discard(task)
    if not task.cancelled() and (exc := task.exception()) is not None:
        # send_email never raises, so this only fires on programmer error.
        logger.error("background email task crashed: %r", exc)


async def drain(timeout: float = 15.0) -> None:
    """Wait for queued email sends to finish (app shutdown, end of a job)."""
    if _PENDING:
        await asyncio.wait(set(_PENDING), timeout=timeout)


async def queue_content(
    to: str,
    content: EmailContent,
    *,
    category: str | None = None,
    headers: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    from_email: str | None = None,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Build-now, send-later: queue an already-rendered email.

    The content was rendered synchronously by the caller (ORM attributes are
    read before the session closes); only the HTTP delivery runs in the
    background. Every queued message gets an ``email_log`` row up front, so
    even a crash between queue and send leaves an auditable ``queued`` row.
    Returns True when a send was queued (provider configured).
    """
    if not get_settings().email_enabled:
        logger.info("email (disabled) to=%s subject=%s", to, content.subject)
        return False
    log_id = await email_log_service.record_queued(
        to_email=to,
        subject=content.subject,
        html=content.html,
        text=content.text,
        category=category,
        headers=headers,
        from_email=from_email,
        idempotency_key=idempotency_key,
        user_id=user_id,
    )
    dispatch(
        send_email(
            to,
            content.subject,
            content.html,
            content.text,
            category=category,
            headers=headers,
            idempotency_key=idempotency_key,
            from_email=from_email,
            log_id=log_id,
            user_id=user_id,
        )
    )
    return True


# ── Transport (Resend) ────────────────────────────────────────────────────


def _payload(
    to: str,
    subject: str,
    html: str,
    text: str | None,
    *,
    category: str | None,
    headers: dict[str, str] | None,
    from_email: str | None = None,
) -> dict[str, Any]:
    """One Resend message object (shared by single-send and batch)."""
    settings = get_settings()
    body: dict[str, Any] = {
        "from": from_email or settings.notifications_from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        body["text"] = text
    if settings.notifications_reply_to:
        body["reply_to"] = settings.notifications_reply_to
    if category:
        body["tags"] = [{"name": "category", "value": category}]
    if headers:
        body["headers"] = headers
    return body


async def _post(
    url: str, json_body: Any, *, idempotency_key: str | None = None
) -> tuple[bool, Any, str | None]:
    """POST to Resend with one retry on transient failures. Never raises.

    Returns ``(ok, response_json_or_None, error_or_None)`` — the body carries
    the provider message id(s) the delivery log links webhook events to.
    """
    settings = get_settings()
    req_headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    if idempotency_key:
        # Resend dedupes on this for 24h — makes retried Stripe webhooks
        # (and our own transient-error retry) safe from double-sends.
        req_headers["Idempotency-Key"] = idempotency_key[:256]
    error: str | None = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=req_headers, json=json_body)
        except Exception as exc:  # network error — retry once, then give up
            error = f"provider unreachable: {exc}"
            if attempt == 1:
                await asyncio.sleep(_RETRY_DELAY_SEC)
                continue
            logger.warning("email provider unreachable (%s)", exc)
            return False, None, error
        if resp.status_code < 400:
            try:
                return True, resp.json(), None
            except Exception:  # non-JSON success body (or a test stub)
                return True, None, None
        error = f"provider error {resp.status_code}: {resp.text[:200]}"
        if resp.status_code in _RETRYABLE and attempt == 1:
            await asyncio.sleep(_RETRY_DELAY_SEC)
            continue
        logger.warning("email %s", error)
        return False, None, error
    return False, None, error


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    *,
    category: str | None = None,
    headers: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    from_email: str | None = None,
    log_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> bool:
    """Send one email now. Returns True if the provider accepted it.

    Every send lands in the delivery log: pass ``log_id`` to advance an
    existing ``queued`` row (the background path), or leave it None and a
    row is created here (direct sends: support, careers, admin tests).
    """
    settings = get_settings()
    if not settings.email_enabled:
        logger.info("email (disabled) to=%s subject=%s", to, subject)
        return False
    if log_id is None:
        log_id = await email_log_service.record_queued(
            to_email=to,
            subject=subject,
            html=html,
            text=text,
            category=category,
            headers=headers,
            from_email=from_email,
            idempotency_key=idempotency_key,
            user_id=user_id,
        )
    ok, data, error = await _post(
        _RESEND_ENDPOINT,
        _payload(
            to,
            subject,
            html,
            text,
            category=category,
            headers=headers,
            from_email=from_email,
        ),
        idempotency_key=idempotency_key,
    )
    if ok:
        provider_id = data.get("id") if isinstance(data, dict) else None
        await email_log_service.mark_sent(log_id, provider_id)
    else:
        logger.warning("email not delivered to=%s subject=%s", to, subject)
        await email_log_service.mark_failed(log_id, error or "send failed")
    return ok


async def send_batch(messages: Sequence[dict[str, Any]]) -> int:
    """Send prebuilt Resend message objects in chunks of 100.

    Each message gets a delivery-log row; provider ids from the batch
    response (order-aligned with the request) link them to webhook events.
    Returns how many messages were in chunks the provider accepted (Resend
    batch is all-or-nothing per call). Note: batch does not support
    attachments/scheduled_at — tags and headers are fine.
    """
    settings = get_settings()
    if not settings.email_enabled:
        logger.info("email batch (disabled) count=%d", len(messages))
        return 0
    accepted = 0
    for start in range(0, len(messages), _BATCH_LIMIT):
        chunk = list(messages[start : start + _BATCH_LIMIT])
        log_ids: list[uuid.UUID | None] = []
        for msg in chunk:
            tags = msg.get("tags") or []
            category = next(
                (t.get("value") for t in tags if t.get("name") == "category"), None
            )
            log_ids.append(
                await email_log_service.record_queued(
                    to_email=(msg.get("to") or [""])[0],
                    subject=msg.get("subject", ""),
                    html=msg.get("html"),
                    text=msg.get("text"),
                    category=category,
                    headers=msg.get("headers"),
                    from_email=msg.get("from"),
                    idempotency_key=None,
                )
            )
        ok, data, error = await _post(_RESEND_BATCH_ENDPOINT, chunk)
        if ok:
            accepted += len(chunk)
            ids = data.get("data") if isinstance(data, dict) else None
            for i, log_id in enumerate(log_ids):
                provider_id = (
                    ids[i].get("id")
                    if isinstance(ids, list)
                    and i < len(ids)
                    and isinstance(ids[i], dict)
                    else None
                )
                await email_log_service.mark_sent(log_id, provider_id)
        else:
            for log_id in log_ids:
                await email_log_service.mark_failed(log_id, error or "batch failed")
    return accepted


# ── send_* wrappers ───────────────────────────────────────────────────────
# Render synchronously, deliver in the background. Return value = "queued"
# (provider configured), not provider acceptance — failures are logged.


async def send_welcome(user: User, *, verify_url: str | None = None) -> bool:
    c = build_welcome(user, verify_url=verify_url)
    return await queue_content(
        user.email, c, category="welcome", user_id=getattr(user, "id", None)
    )


async def send_verify_email(user: User, verify_url: str) -> bool:
    c = build_verify_email(user, verify_url)
    return await queue_content(
        user.email, c, category="account", user_id=getattr(user, "id", None)
    )


async def send_admin_granted(user: User) -> bool:
    c = build_admin_granted(user)
    return await queue_content(
        user.email, c, category="admin", user_id=getattr(user, "id", None)
    )


async def send_ban_notice(user: User, reason: str | None) -> bool:
    c = build_ban_notice(user, reason)
    return await queue_content(
        user.email, c, category="account", user_id=getattr(user, "id", None)
    )


async def send_password_changed(user: User) -> bool:
    c = build_password_changed(user)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_mfa_enabled(user: User) -> bool:
    c = build_mfa_enabled(user)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_mfa_disabled(user: User) -> bool:
    c = build_mfa_disabled(user)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_password_reset(user: User, reset_url: str) -> bool:
    c = build_password_reset(user, reset_url)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_reset_unavailable(user: User) -> bool:
    c = build_reset_unavailable(user)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_new_sign_in(
    user: User,
    *,
    device: str | None = None,
    location: str | None = None,
    ip: str | None = None,
    when: datetime | None = None,
) -> bool:
    c = build_new_sign_in(user, device=device, location=location, ip=ip, when=when)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_account_locked(
    user: User, *, minutes: int, attempts: int, when: datetime | None = None
) -> bool:
    c = build_account_locked(user, minutes=minutes, attempts=attempts, when=when)
    return await queue_content(
        user.email, c, category="security", user_id=getattr(user, "id", None)
    )


async def send_pro_activated(user: User, *, idempotency_key: str | None = None) -> bool:
    c = build_pro_activated(user)
    return await queue_content(
        user.email,
        c,
        category="billing",
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_pro_canceled(user: User, *, idempotency_key: str | None = None) -> bool:
    c = build_pro_canceled(user)
    return await queue_content(
        user.email,
        c,
        category="billing",
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_payment_failed(
    user: User,
    *,
    amount_usd: Decimal | float | None = None,
    attempt: int = 1,
    max_attempts: int = 4,
    next_attempt: datetime | None = None,
    grace_days: int | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Dunning notice. Keyed on the Stripe invoice+attempt by the caller so a
    replayed webhook can't double-send."""
    c = build_payment_failed(
        user,
        amount_usd=amount_usd,
        attempt=attempt,
        max_attempts=max_attempts,
        next_attempt=next_attempt,
        grace_days=grace_days,
    )
    return await queue_content(
        user.email,
        c,
        category="billing",
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_pro_expiring(
    user: User,
    *,
    ends_on: datetime,
    days_left: int,
    idempotency_key: str | None = None,
) -> bool:
    c = build_pro_expiring(user, ends_on=ends_on, days_left=days_left)
    return await queue_content(
        user.email,
        c,
        category="billing",
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


#: Once per account — guarded with ``email_log_service.has_ever_sent`` on this
#: category, so it must not be shared with any other email.
CATEGORY_VAULT_FULL = "vault-full"
#: Once per (user, set) — guarded with ``email_log_service.has_sent_key`` on
#: the idempotency key, since the milestone recurs for every set completed.
CATEGORY_SET_COMPLETE = "set-complete"


async def send_free_limit_reached(
    user: User, *, card_count: int, limit: int, idempotency_key: str | None = None
) -> bool:
    """The free-vault ceiling notice. One-shot — the caller guards with
    ``email_log_service.has_ever_sent(db, user.id, CATEGORY_VAULT_FULL)``."""
    c = build_free_limit_reached(user, card_count=card_count, limit=limit)
    return await queue_content(
        user.email,
        c,
        category=CATEGORY_VAULT_FULL,
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_set_completed(
    user: User,
    *,
    set_name: str,
    set_total: int,
    series_name: str | None = None,
    set_id: str | None = None,
    image_url: str | None = None,
    total_value_usd: Decimal | float | None = None,
    idempotency_key: str | None = None,
) -> bool:
    c = build_set_completed(
        user,
        set_name=set_name,
        set_total=set_total,
        series_name=series_name,
        set_id=set_id,
        image_url=image_url,
        total_value_usd=total_value_usd,
    )
    return await queue_content(
        user.email,
        c,
        category=CATEGORY_SET_COMPLETE,
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_price_alert(
    email: str,
    *,
    card_name: str,
    set_name: str | None,
    condition: str,
    threshold_usd: Decimal,
    price_usd: Decimal,
    card_id: Any,
    image_url: str | None = None,
    history: list[float] | None = None,
) -> bool:
    """The 'your alert fired' email — the whole point of setting an alert."""
    c = build_price_alert(
        card_name=card_name,
        set_name=set_name,
        condition=condition,
        threshold_usd=threshold_usd,
        price_usd=price_usd,
        card_id=card_id,
        image_url=image_url,
        history=history,
    )
    return await queue_content(email, c, category="price-alert")


async def send_statement_ready(
    user: User,
    *,
    title: str,
    idempotency_key: str | None = None,
    total_value_usd: float | None = None,
    delta_pct: float | None = None,
    card_count: int | None = None,
    series: list[float] | None = None,
) -> bool:
    c = build_statement_ready(
        user,
        title=title,
        total_value_usd=total_value_usd,
        delta_pct=delta_pct,
        card_count=card_count,
        series=series,
    )
    return await queue_content(
        user.email,
        c,
        category="statement",
        idempotency_key=idempotency_key,
        user_id=getattr(user, "id", None),
    )


async def send_waitlist_confirmation(
    email: str, *, name: str | None, position: int
) -> bool:
    """Confirm a Loupe Scanner waitlist signup and share their place in line."""
    c = build_waitlist_confirmation(name=name, position=position)
    return await queue_content(email, c, category="waitlist")


async def send_waitlist_invite(email: str, *, name: str | None) -> bool:
    """The 'your spot opened up' email promised by the confirmation."""
    c = build_waitlist_invite(name=name)
    return await queue_content(email, c, category="waitlist")


async def send_support_message(
    user: User,
    *,
    subject: str,
    body_text: str,
    cta: tuple[str, str] | None = None,
) -> bool:
    """One-to-one message from the support team. Sent *synchronously* — the
    admin on the other end wants the real provider verdict, and it goes out
    from ``SUPPORT_FROM_EMAIL`` when configured so replies land in the
    support inbox."""
    c = build_support_message(
        recipient_name=user.display_name, subject=subject, body_text=body_text, cta=cta
    )
    return await send_email(
        user.email,
        c.subject,
        c.html,
        c.text,
        category="support",
        from_email=get_settings().support_sender or None,
    )


# ── Announcement fan-out (batch) ──────────────────────────────────────────


def _one_click_headers(unsub_url: str) -> dict[str, str]:
    return {
        "List-Unsubscribe": f"<{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


async def send_blog_announcement(
    recipients: Sequence[tuple[str, str]],
    *,
    title: str,
    excerpt: str,
    slug: str,
    cover_image_url: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    read_minutes: int | None = None,
) -> int:
    """Announce a newly published post to ``(email, unsubscribe_url)`` pairs.

    Announcement-class mail, so every message carries a one-click
    ``List-Unsubscribe`` header and a footer opt-out link pointing at that
    recipient's signed unsubscribe URL. Sent via the batch endpoint.
    Returns how many messages were accepted.
    """
    messages: list[dict[str, Any]] = []
    for email, unsub_url in recipients:
        c = build_blog_announcement(
            title=title,
            excerpt=excerpt,
            slug=slug,
            unsub_url=unsub_url,
            cover_image_url=cover_image_url,
            tag=tag,
            author=author,
            read_minutes=read_minutes,
        )
        messages.append(
            _payload(
                email,
                c.subject,
                c.html,
                c.text,
                category="announcement",
                headers=_one_click_headers(unsub_url),
            )
        )
    sent = await send_batch(messages)
    logger.info(
        "blog announcement '%s' → %d/%d recipients", title, sent, len(recipients)
    )
    return sent


async def send_custom_announcement(
    recipients: Sequence[tuple[str, str]],
    *,
    subject: str,
    heading: str,
    body_text: str,
    cta: tuple[str, str] | None,
) -> int:
    """Batch-send an admin-composed announcement to ``(email, unsub_url)``
    pairs, with the same one-click unsubscribe treatment as blog posts."""
    messages: list[dict[str, Any]] = []
    for email, unsub_url in recipients:
        c = build_custom_announcement(
            subject=subject,
            heading=heading,
            body_text=body_text,
            cta=cta,
            unsub_url=unsub_url,
        )
        messages.append(
            _payload(
                email,
                c.subject,
                c.html,
                c.text,
                category="announcement",
                headers=_one_click_headers(unsub_url),
            )
        )
    sent = await send_batch(messages)
    logger.info(
        "custom announcement '%s' → %d/%d recipients", subject, sent, len(recipients)
    )
    return sent


class DigestRecipient(NamedTuple):
    """One user's slice of a digest run — everything the template needs.

    The caller (the digest cron) computes these from the portfolio snapshots
    it already reads, so the fan-out itself does no database work.
    """

    user: User
    unsub_url: str
    total_value_usd: Decimal | float
    delta_pct: float
    delta_usd: Decimal | float
    card_count: int
    series: list[float] | None = None
    top_movers: list[tuple[str, float]] | None = None


async def send_portfolio_digest(
    recipients: Sequence[DigestRecipient], *, period_label: str = "This week"
) -> int:
    """Batch-send the recurring portfolio digest.

    Digest mail is recurring and non-transactional, so — like announcements —
    every message carries a one-click ``List-Unsubscribe`` header plus the
    footer opt-out link. Returns how many messages the provider accepted.
    """
    messages: list[dict[str, Any]] = []
    for r in recipients:
        c = build_portfolio_digest(
            r.user,
            period_label=period_label,
            total_value_usd=r.total_value_usd,
            delta_pct=r.delta_pct,
            delta_usd=r.delta_usd,
            card_count=r.card_count,
            unsub_url=r.unsub_url,
            series=r.series,
            top_movers=r.top_movers,
        )
        messages.append(
            _payload(
                r.user.email,
                c.subject,
                c.html,
                c.text,
                category="digest",
                headers=_one_click_headers(r.unsub_url),
            )
        )
    sent = await send_batch(messages)
    logger.info("portfolio digest (%s) → %d/%d", period_label, sent, len(recipients))
    return sent


__all__ = [
    "CATEGORY_SET_COMPLETE",
    "CATEGORY_VAULT_FULL",
    "DigestRecipient",
    "EmailContent",
    "build_account_locked",
    "build_admin_granted",
    "build_ban_notice",
    "build_blog_announcement",
    "build_custom_announcement",
    "build_free_limit_reached",
    "build_mfa_disabled",
    "build_mfa_enabled",
    "build_new_sign_in",
    "build_password_changed",
    "build_password_reset",
    "build_payment_failed",
    "build_portfolio_digest",
    "build_price_alert",
    "build_pro_activated",
    "build_pro_canceled",
    "build_pro_expiring",
    "build_reset_unavailable",
    "build_set_completed",
    "build_statement_ready",
    "build_support_message",
    "build_verify_email",
    "build_waitlist_confirmation",
    "build_waitlist_invite",
    "build_welcome",
    "dispatch",
    "drain",
    "queue_content",
    "render_email",
    "send_account_locked",
    "send_admin_granted",
    "send_ban_notice",
    "send_batch",
    "send_blog_announcement",
    "send_custom_announcement",
    "send_email",
    "send_free_limit_reached",
    "send_mfa_disabled",
    "send_mfa_enabled",
    "send_new_sign_in",
    "send_password_changed",
    "send_password_reset",
    "send_payment_failed",
    "send_portfolio_digest",
    "send_price_alert",
    "send_pro_activated",
    "send_pro_canceled",
    "send_pro_expiring",
    "send_reset_unavailable",
    "send_set_completed",
    "send_statement_ready",
    "send_support_message",
    "send_verify_email",
    "send_waitlist_confirmation",
    "send_waitlist_invite",
    "send_welcome",
]
