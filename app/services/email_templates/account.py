"""Account lifecycle templates: welcome, email confirmation, ban, admin grant.

Each has its own identity: welcome leads with the product promise (not a
greeting), verification is a single centered action, the admin grant reads
like the dev portal (terminal panel), and the ban notice is unmistakably
serious (rose).
"""

from __future__ import annotations

from app.models.user import User
from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    callout,
    check_list,
    display_name,
    esc,
    render_email,
)


def _journey_tiles() -> str:
    """SCAN → TRACK → ALERT as three dark vault tiles with mint step numbers."""
    steps = [("1", "Scan"), ("2", "Track"), ("3", "Alert")]
    cells = "".join(
        f'<td width="33%" style="padding:5px;">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0"><tr><td align="center" style="background:{theme.BAND_BG};'
        f'border-radius:12px;padding:18px 8px;">'
        f'<p style="margin:0;font-size:22px;font-weight:600;'
        f'color:{theme.BAND_MINT};font-family:{theme.FONT_SERIF};">{num}</p>'
        f'<p style="margin:6px 0 0;font-size:11px;font-weight:700;'
        f"letter-spacing:0.14em;text-transform:uppercase;color:#f5f5f7;"
        f'font-family:{theme.FONT};">{label}</p>'
        f"</td></tr></table></td>"
        for num, label in steps
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:16px 0 6px;"><tr>{cells}</tr></table>'
    )


def build_welcome(user: User, *, verify_url: str | None = None) -> EmailContent:
    """Welcome email. Password signups get a 'confirm your email' CTA folded
    in (one email, not two); social signups arrive pre-verified and get a
    plain 'Open Loupe'."""
    confirm = (
        callout(
            "One quick thing: <strong>confirm your email</strong> below so "
            "price alerts, statements, and account notices reach you reliably.",
            tone="mint",
        )
        if verify_url
        else ""
    )
    body = (
        f"<p>Hi {esc(display_name(user))} — welcome to Loupe. Three steps and "
        f"your shoebox is a portfolio:</p>"
        + _journey_tiles()
        + check_list(
            [
                "<strong>Scan</strong> a card and it's identified, priced, and vaulted",
                "<strong>Track</strong> live, grade-aware valuations across markets",
                "<strong>Alert</strong> on price moves; monthly statements land automatically",
            ]
        )
        + f"{confirm}"
    )
    cta = (
        ("Confirm your email", verify_url) if verify_url else ("Open Loupe", app_url())
    )
    html, text = render_email(
        "Your collection just got a portfolio.",
        body,
        cta,
        preheader="Your collection just got a portfolio.",
        eyebrow="Welcome to Loupe",
    )
    return EmailContent("Welcome to Loupe", html, text)


def _check_circle() -> str:
    """A big mint check medallion — the single-action moment."""
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'align="center" style="margin:6px auto 14px;"><tr>'
        f'<td align="center" style="width:76px;height:76px;border-radius:999px;'
        f"background:{theme.MINT_TINT};border:2px solid {theme.MINT};"
        f"font-size:34px;line-height:76px;color:{theme.MINT};"
        f'font-family:{theme.FONT};">&#10003;</td>'
        f"</tr></table>"
    )


def build_verify_email(user: User, verify_url: str) -> EmailContent:
    """Standalone confirmation request (the 'resend verification' email)."""
    from app.services.email_templates.base import chip

    body = (
        _check_circle() + f'<p style="margin:0 0 12px;text-align:center;">'
        f"{chip('Takes two seconds', tone='neutral')}</p>"
        + f"<p>Hi {esc(display_name(user))} — confirm this is your email "
        "address and you're all set. Price alerts, statements, and account "
        "notices will reach you reliably.</p>"
        f'<p style="font-size:13px;color:{theme.INK_DIM};">If you didn\'t '
        "create a Loupe account, you can ignore this email.</p>"
    )
    html, text = render_email(
        "One tap and you're set.",
        body,
        ("Confirm your email", verify_url),
        preheader="One tap to confirm your email address.",
        eyebrow="Confirm your email",
    )
    return EmailContent("Confirm your Loupe email", html, text)


def build_admin_granted(user: User) -> EmailContent:
    """Dev-portal access — styled like the portal itself: a terminal panel."""
    rules = (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:18px 0 8px;"><tr>'
        f'<td style="background:{theme.BAND_BG};border-radius:12px;'
        f"padding:18px 22px;font-family:{theme.FONT_MONO};font-size:13px;"
        f'line-height:2;color:#f5f5f7;">'
        f'<span style="color:{theme.BAND_MINT};">$</span> actions are '
        f"audit-logged &mdash; who, what, when, IP<br>"
        f'<span style="color:{theme.BAND_MINT};">$</span> user data is '
        f"confidential &mdash; access only what you need<br>"
        f'<span style="color:{theme.BAND_MINT};">$</span> bans &amp; deletions '
        f"are powerful &mdash; double-check before you act"
        f"</td></tr></table>"
    )
    body = (
        f"<p>Hi {esc(display_name(user))} — you've been granted "
        "<strong>admin access</strong> to the Loupe developer portal.</p>" + rules
    )
    html, text = render_email(
        "Welcome to the dev portal.",
        body,
        ("Open the portal", f"{app_url()}/admin"),
        preheader="You now have access to the Loupe developer portal.",
        eyebrow="Dev portal access",
    )
    return EmailContent("You're now a Loupe admin", html, text)


def build_ban_notice(user: User, reason: str | None) -> EmailContent:
    from app.services.email_templates.base import chip, panel

    why = (
        callout(f"<strong>Reason:</strong> {esc(reason)}", tone="rose")
        if reason
        else ""
    )
    body = (
        f"<p>Hi {esc(display_name(user))} — your Loupe account has been "
        "suspended and you've been signed out of every device.</p>"
        + panel(
            [
                ("Status", chip("Suspended", tone="rose")),
                ("Devices", "Signed out everywhere"),
                ("Your data", "Preserved while suspended"),
                ("Appeals", "Reply to this email"),
            ]
        )
        + f"{why}"
        + "<p>If you believe this is a mistake, reply to this email and we'll "
        "take a look.</p>"
    )
    html, text = render_email(
        "Your account was suspended.",
        body,
        preheader="Your account has been suspended.",
        eyebrow="Account notice",
        eyebrow_color=theme.ROSE,
    )
    return EmailContent("Your Loupe account was suspended", html, text)


__all__ = [
    "build_admin_granted",
    "build_ban_notice",
    "build_verify_email",
    "build_welcome",
]
