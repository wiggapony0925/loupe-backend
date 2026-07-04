"""Security notices: password changed/reset, 2FA on/off.

One family, four moods, each with a visual: the reset carries a dark
"authorization" tile, the change notice a what/when event panel, and the 2FA
pair show your protection level as a meter that fills (or empties). All share
the SECURITY eyebrow so they're instantly recognizable in a crowded inbox.
"""

from __future__ import annotations

from app.models.user import User
from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    callout,
    chip,
    display_name,
    esc,
    panel,
    progress_steps,
    render_email,
)

_WASNT_YOU = (
    "If this wasn't you, secure your account now: change your password and "
    "sign out of all devices from "
    '<a href="{settings_url}" target="_blank" '
    f'style="{theme.QUIET_LINK_STYLE}">Settings</a>, '
    "or reply to this email and we'll help."
)


def _settings_url() -> str:
    return f"{app_url()}/app/settings"


def build_password_changed(user: User) -> EmailContent:
    body = (
        f"<p>Hi {esc(display_name(user))} — your account password was just "
        "changed.</p>"
        + panel(
            [
                ("What changed", "Account password"),
                ("Other sessions", "Signed out everywhere"),
                ("This device", "Still signed in"),
            ]
        )
        + callout(_WASNT_YOU.format(settings_url=_settings_url()), tone="rose")
    )
    html, text = render_email(
        "Your password was changed.",
        body,
        preheader="Your password was just changed.",
        eyebrow="Security notice",
        eyebrow_color=theme.ROSE,
    )
    return EmailContent("Your Loupe password was changed", html, text)


def build_mfa_enabled(user: User) -> EmailContent:
    body = (
        f"<p>Hi {esc(display_name(user))} — two-factor authentication was "
        "just <strong>enabled</strong> on your account.</p>"
        + progress_steps(["Password", "Authenticator"], 1)
        + f'<p style="margin:4px 0 10px;text-align:center;">'
        f"{chip('Protection: strong', tone='mint')}</p>"
        + callout(
            "From now on, signing in takes your password <em>plus</em> a code "
            "from your authenticator app. Keep your backup codes somewhere safe.",
            tone="mint",
        )
        + callout(_WASNT_YOU.format(settings_url=_settings_url()), tone="rose")
    )
    html, text = render_email(
        "Two-factor is on.",
        body,
        preheader="2FA was just enabled on your account.",
        eyebrow="Security upgrade",
    )
    return EmailContent("Two-factor authentication is on", html, text)


def build_mfa_disabled(user: User) -> EmailContent:
    body = (
        f"<p>Hi {esc(display_name(user))} — two-factor authentication was "
        "just <strong>disabled</strong> on your account.</p>"
        + progress_steps(["Password", "Authenticator"], 0)
        + f'<p style="margin:4px 0 10px;text-align:center;">'
        f"{chip('Protection: basic', tone='amber')}</p>"
        + callout(
            "Your password alone now signs you in. You can re-enable 2FA any "
            "time from Settings.",
            tone="amber",
        )
        + callout(_WASNT_YOU.format(settings_url=_settings_url()), tone="rose")
    )
    html, text = render_email(
        "Two-factor is off.",
        body,
        preheader="2FA was just disabled on your account.",
        eyebrow="Security change",
        eyebrow_color="#b8860b",
    )
    return EmailContent("Two-factor authentication is off", html, text)


def _authorization_tile() -> str:
    """A dark 'reset authorized' tile — the vault-door moment."""
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:18px 0 8px;"><tr>'
        f'<td align="center" style="background:{theme.BAND_BG};border-radius:14px;'
        f'padding:22px 20px;">'
        f'<p style="margin:0;font-size:24px;letter-spacing:0.5em;'
        f'color:{theme.BAND_MINT};font-family:{theme.FONT_MONO};">&bull;&bull;'
        f"&bull;&bull;&bull;&bull;&bull;&bull;</p>"
        f'<p style="margin:10px 0 0;font-size:10px;font-weight:700;'
        f"letter-spacing:0.18em;text-transform:uppercase;color:{theme.BAND_MUTED};"
        f'font-family:{theme.FONT};">Password reset authorized</p>'
        f"</td></tr></table>"
    )


def build_password_reset(user: User, reset_url: str) -> EmailContent:
    body = (
        f"<p>Hi {esc(display_name(user))} — someone (hopefully you) asked to "
        "reset the password for this Loupe account.</p>"
        + _authorization_tile()
        + f'<p style="margin:14px 0 4px;text-align:center;">'
        f"{chip('Link expires in 30 minutes', tone='amber')}"
        f"&nbsp;{chip('Single use', tone='neutral')}</p>"
        "<p>Using the link signs you out everywhere else.</p>"
        f'<p style="font-size:13px;color:{theme.INK_DIM};">If you didn\'t ask '
        "for this, you can safely ignore this email — your password is "
        "unchanged.</p>"
    )
    html, text = render_email(
        "Reset your password.",
        body,
        ("Choose a new password", reset_url),
        preheader="Your password reset link — good for 30 minutes.",
        eyebrow="Password reset",
    )
    return EmailContent("Reset your Loupe password", html, text)


def _provider_buttons() -> str:
    """The two social sign-in pills, linking to the login page."""
    login = f"{app_url()}/login"
    cell = (
        '<td width="50%" style="padding:5px;"><table role="presentation" '
        'width="100%" cellspacing="0" cellpadding="0" border="0"><tr>'
        f'<td align="center" style="background:{theme.CARD_BG};border:1px solid '
        f'{theme.LINE};border-radius:999px;">'
        f'<a href="{login}" target="_blank" style="display:inline-block;'
        f"padding:12px 10px;color:{theme.INK};text-decoration:none;"
        f'font-weight:600;font-size:14px;font-family:{theme.FONT};">{{label}}</a>'
        "</td></tr></table></td>"
    )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'border="0" style="margin:16px 0 8px;"><tr>'
        + cell.format(label="&#63743; Continue with Apple")
        + cell.format(label="G &middot; Continue with Google")
        + "</tr></table>"
    )


def build_reset_unavailable(user: User) -> EmailContent:
    body = (
        f"<p>Hi {esc(display_name(user))} — a password reset was requested "
        "for this email address, but this Loupe account signs in with "
        "<strong>Apple or Google</strong>, so there's no password to reset.</p>"
        + _provider_buttons()
        + callout(
            "Just use the same <em>Continue with Apple / Google</em> button "
            "you signed up with.",
            tone="mint",
        )
        + f'<p style="font-size:13px;color:{theme.INK_DIM};">If you didn\'t '
        "request this, no action is needed.</p>"
    )
    html, text = render_email(
        "No password here — and that's fine.",
        body,
        ("Open Loupe", f"{app_url()}/login"),
        preheader="This account signs in with Apple or Google.",
        eyebrow="Sign-in help",
    )
    return EmailContent("How to sign in to Loupe", html, text)


__all__ = [
    "build_mfa_disabled",
    "build_mfa_enabled",
    "build_password_changed",
    "build_password_reset",
    "build_reset_unavailable",
]
