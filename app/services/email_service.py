"""Transactional email — one place that talks to the provider (Resend).

`send_email` is the low-level sender (a single HTTPS POST, best-effort, never
raises). The `send_*` helpers build branded lifecycle emails: welcome on
signup, ban notice, admin-granted, and blog announcements. All are no-ops
(logged only) until ``RESEND_API_KEY`` + ``NOTIFICATIONS_FROM_EMAIL`` are set,
so callers can fire them unconditionally without guarding.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.models.user import User
from app.utils.logger import get_logger

logger = get_logger("email")

_RESEND_ENDPOINT = "https://api.resend.com/emails"


def _app_url() -> str:
    return get_settings().app_public_url.rstrip("/")


def _wrap(heading: str, body_html: str, cta: tuple[str, str] | None = None) -> str:
    """Brand wrapper: heading + body + optional (label, href) CTA button."""
    button = (
        f'<p style="margin:24px 0;"><a href="{cta[1]}" style="background:#00a86e;'
        f"color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;"
        f'font-weight:600;">{cta[0]}</a></p>'
        if cta
        else ""
    )
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        f'max-width:520px;margin:0 auto;color:#1c1c1e;line-height:1.6;">'
        f'<h2 style="color:#0b0b0d;">{heading}</h2>'
        f"{body_html}{button}"
        f'<hr style="border:0;border-top:1px solid #e5e5ea;margin:28px 0 12px;">'
        f'<p style="color:#6e6e73;font-size:13px;">— The Loupe team</p>'
        f"</div>"
    )


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email. Returns True if the provider accepted it, else False."""
    settings = get_settings()
    if not settings.email_enabled:
        logger.info("email (disabled) to=%s subject=%s", to, subject)
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": settings.notifications_from_email,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                },
            )
        if resp.status_code >= 400:
            logger.warning("email provider error %s to=%s", resp.status_code, to)
            return False
        return True
    except Exception as exc:  # never let email break the caller
        logger.warning("email failed (%s) to=%s", exc, to)
        return False


def _name(user: User) -> str:
    return user.display_name or (user.email or "there").split("@", 1)[0]


async def send_welcome(user: User) -> bool:
    body = (
        "<p>Welcome to Loupe — your collection just got a portfolio.</p>"
        "<p>Browse the market, add cards to your vault, and track live, "
        "grade-aware valuations on web and mobile.</p>"
    )
    return await send_email(
        user.email,
        "Welcome to Loupe",
        _wrap(f"Hi {_name(user)},", body, ("Open Loupe", _app_url())),
    )


async def send_admin_granted(user: User) -> bool:
    body = (
        "<p>You've been granted <strong>admin access</strong> to the Loupe "
        "developer portal.</p>"
        "<p><strong>A few ground rules:</strong></p>"
        "<ul>"
        "<li>Admin actions are audit-logged (who, what, when, IP).</li>"
        "<li>Treat user data as confidential — access only what you need.</li>"
        "<li>Bans and deletions are powerful; double-check before you act.</li>"
        "</ul>"
    )
    return await send_email(
        user.email,
        "You're now a Loupe admin",
        _wrap(f"Hi {_name(user)},", body, ("Open the portal", f"{_app_url()}/admin")),
    )


async def send_ban_notice(user: User, reason: str | None) -> bool:
    why = f"<p><strong>Reason:</strong> {reason}</p>" if reason else ""
    body = (
        "<p>Your Loupe account has been suspended and you've been signed out.</p>"
        f"{why}"
        "<p>If you believe this is a mistake, reply to this email and we'll take a look.</p>"
    )
    return await send_email(
        user.email,
        "Your Loupe account was suspended",
        _wrap(f"Hi {_name(user)},", body),
    )


async def send_blog_announcement(
    emails: list[str], *, title: str, excerpt: str, slug: str
) -> int:
    """Announce a newly published post. Returns how many were accepted."""
    url = f"{_app_url()}/blog/{slug}"
    html = _wrap(
        title,
        f"<p>{excerpt or 'A new post is live on the Loupe blog.'}</p>",
        ("Read the post", url),
    )
    sent = 0
    for email in emails:
        if await send_email(email, f"New from Loupe: {title}", html):
            sent += 1
    logger.info("blog announcement '%s' → %d/%d recipients", title, sent, len(emails))
    return sent


__all__ = [
    "send_admin_granted",
    "send_ban_notice",
    "send_blog_announcement",
    "send_email",
    "send_welcome",
]
