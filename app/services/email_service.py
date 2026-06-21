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


# Brand palette — mirrors the loupe-frontend / loupe-web theme tokens.
_INK = "#0E1117"
_INK_DIM = "#5B6470"
_LINE = "#E5E8EC"
_PAGE_BG = "#F4F5F7"
_MINT = "#00C896"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _wrap(heading: str, body_html: str, cta: tuple[str, str] | None = None) -> str:
    """Themed Loupe email: dark header band, white content card, mint CTA.

    Inline styles only (email clients strip <style>), a 560px card on a soft
    page background, dark-ink-on-mint button text for contrast.
    """
    button = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="margin:26px 0 6px;"><tr><td '
        f'style="border-radius:999px;background:{_MINT};">'
        f'<a href="{cta[1]}" style="display:inline-block;padding:13px 26px;'
        f"color:{_INK};text-decoration:none;font-weight:700;font-size:15px;"
        f'font-family:{_FONT};">{cta[0]} &rarr;</a></td></tr></table>'
        if cta
        else ""
    )
    app_url = _app_url()
    return (
        f'<div style="background:{_PAGE_BG};padding:28px 16px;font-family:{_FONT};">'
        f'<div style="max-width:560px;margin:0 auto;background:#ffffff;'
        f"border:1px solid {_LINE};border-radius:18px;overflow:hidden;"
        f'box-shadow:0 8px 24px rgba(16,24,40,0.06);">'
        f'<div style="background:{_INK};padding:20px 28px;">'
        f'<span style="color:{_MINT};font-size:17px;font-weight:800;'
        f'letter-spacing:0.3px;">&#9670; LOUPE</span>'
        f'<span style="color:#8A93A0;font-size:12px;font-weight:600;'
        f'float:right;padding-top:5px;">PORTFOLIO &middot; MARKETS</span>'
        f"</div>"
        f'<div style="padding:30px 28px 8px;color:{_INK};line-height:1.62;'
        f'font-size:15px;">'
        f'<h1 style="margin:0 0 14px;color:{_INK};font-size:22px;'
        f'font-weight:800;letter-spacing:-0.02em;">{heading}</h1>'
        f"{body_html}{button}"
        f"</div>"
        f'<div style="border-top:1px solid {_LINE};padding:18px 28px 24px;'
        f'color:{_INK_DIM};font-size:13px;line-height:1.6;">'
        f'<p style="margin:0 0 6px;font-weight:600;color:{_INK};">'
        f"&mdash; The Loupe team</p>"
        f'<p style="margin:0;">Track every card like a position &mdash; on the '
        f'web and in your pocket. <a href="{app_url}" '
        f'style="color:{_MINT};text-decoration:none;font-weight:600;">loupe</a></p>'
        f"</div>"
        f"</div></div>"
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


async def send_waitlist_confirmation(
    email: str, *, name: str | None, position: int
) -> bool:
    """Confirm a Loupe Scanner waitlist signup and share their place in line."""
    greeting = f"Hi {name}," if name else "Hi there,"
    body = (
        "<p>You're on the list for the <strong>Loupe Scanner</strong> — the "
        "fastest way to turn a shoebox of cards into a live, grade-aware "
        "portfolio.</p>"
        f"<p>You're <strong>#{position}</strong> in line. We'll email you a "
        "private purchase link the moment your spot opens up.</p>"
        "<p>In the meantime, you can keep tracking your collection on web and "
        "mobile.</p>"
    )
    return await send_email(
        email,
        "You're on the Loupe Scanner waitlist",
        _wrap(greeting, body, ("Open Loupe", _app_url())),
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
    "send_waitlist_confirmation",
    "send_welcome",
]
