"""Loupe Scanner waitlist: signup confirmation and the invite it promises.

The confirmation makes your place in line the hero (big serif number); the
invite is a ticket — dashed border, reserved seat, momentum.
"""

from __future__ import annotations

from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    big_quote,
    chip,
    esc,
    render_email,
)


def build_waitlist_confirmation(*, name: str | None, position: int) -> EmailContent:
    greeting = f"Hi {esc(name)} — you're" if name else "You're"
    body = (
        big_quote(f"#{position}", "your place in line", color=theme.MINT)
        + f"<p>{greeting} on the list for the <strong>Loupe Scanner</strong> "
        "— the fastest way to turn a shoebox of cards into a live, "
        "grade-aware portfolio.</p>"
        "<p>We'll email you a private purchase link the moment your spot "
        "opens up. In the meantime, keep tracking your collection on web "
        "and mobile.</p>"
    )
    html, text = render_email(
        "You're in line.",
        body,
        ("Open Loupe", app_url()),
        preheader=f"You're #{position} in line for the Loupe Scanner.",
        eyebrow="Scanner waitlist",
    )
    return EmailContent("You're on the Loupe Scanner waitlist", html, text)


def build_waitlist_invite(*, name: str | None) -> EmailContent:
    greeting = f"Hi {esc(name)} — good" if name else "Good"
    ticket = (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:20px 0 8px;"><tr>'
        f'<td align="center" style="border:2px dashed {theme.MINT};'
        f'border-radius:14px;padding:24px 20px;background:{theme.MINT_TINT};">'
        f'<p style="margin:0 0 6px;font-size:11px;font-weight:700;'
        f"letter-spacing:0.18em;text-transform:uppercase;color:{theme.MINT};"
        f'font-family:{theme.FONT};">Reserved for you</p>'
        f'<p style="margin:0;font-size:26px;font-weight:600;color:{theme.INK};'
        f'letter-spacing:-0.01em;font-family:{theme.FONT_SERIF};">Loupe Scanner</p>'
        f'<p style="margin:10px 0 0;">{chip("Limited-time invitation", tone="amber")}</p>'
        f"</td></tr></table>"
    )
    body = (
        ticket + f"<p>{greeting} news — <strong>your spot is open</strong>. You can "
        "now order your Loupe Scanner.</p>"
        "<p>Spots are released in small batches, so this invitation is "
        "reserved for you for a limited time.</p>"
    )
    html, text = render_email(
        "Your spot is open.",
        body,
        ("Order your Scanner", f"{app_url()}/scanner"),
        preheader="Your Loupe Scanner spot just opened up.",
        eyebrow="Scanner waitlist",
    )
    return EmailContent("Your Loupe Scanner spot is open", html, text)


__all__ = ["build_waitlist_confirmation", "build_waitlist_invite"]
