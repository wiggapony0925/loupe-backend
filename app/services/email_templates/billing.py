"""Billing templates: Pro activation and cancellation (transition-only).

Pro activation is the celebration email — it renders a dark membership card
(the app's OLED chrome with the bright-mint PRO wordmark). Cancellation shows
the same card, dimmed and PAUSED: your membership isn't gone, it's waiting.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.user import User
from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    display_name,
    esc,
    render_email,
    stat_tiles,
)


def _member_since(user: User) -> int:
    stamp = getattr(user, "pro_since", None) or getattr(user, "created_at", None)
    return stamp.year if stamp else datetime.now(UTC).year


def _membership_card(user: User, *, active: bool) -> str:
    """The Pro 'card' — dark + bright mint when active, dimmed when paused."""
    if active:
        bg, word, name_c, meta_c = (
            theme.BAND_BG,
            theme.BAND_MINT,
            "#f5f5f7",
            theme.BAND_MUTED,
        )
        badge_bg, badge_c, badge_txt = ("#12291e", theme.BAND_MINT, "ACTIVE")
    else:
        bg, word, name_c, meta_c = (
            theme.SUNKEN_BG,
            theme.INK_DIM,
            theme.INK_MUTED,
            theme.INK_DIM,
        )
        badge_bg, badge_c, badge_txt = ("#e2e2e6", theme.INK_MUTED, "PAUSED")
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:20px 0 8px;"><tr>'
        f'<td style="background:{bg};border-radius:16px;padding:24px 26px;">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0"><tr>'
        f'<td style="color:{word};font-size:14px;font-weight:800;'
        f'letter-spacing:0.16em;font-family:{theme.FONT};">&#9670;&nbsp;LOUPE&nbsp;PRO</td>'
        f'<td align="right"><span style="display:inline-block;padding:4px 12px;'
        f"border-radius:999px;background:{badge_bg};color:{badge_c};font-size:10px;"
        f"font-weight:700;letter-spacing:0.14em;"
        f'font-family:{theme.FONT};">{badge_txt}</span></td>'
        f"</tr></table>"
        f'<p style="margin:24px 0 0;color:{name_c};font-size:23px;font-weight:600;'
        f'letter-spacing:-0.01em;font-family:{theme.FONT_SERIF};">'
        f"{esc(display_name(user))}</p>"
        f'<p style="margin:8px 0 0;color:{meta_c};font-size:10px;font-weight:600;'
        f'letter-spacing:0.16em;font-family:{theme.FONT};">MEMBER SINCE '
        f"{_member_since(user)} &middot; UNLIMITED VAULT</p>"
        f"</td></tr></table>"
    )


def build_pro_activated(user: User) -> EmailContent:
    body = (
        _membership_card(user, active=True)
        + "<p>Your <strong>Loupe Pro</strong> subscription is active — "
        "everything is unlocked:</p>"
        + stat_tiles(
            [
                ("&#8734;", "cards in your vault"),
                ("Full", "price history"),
                ("Priority", "scanning &amp; grading"),
                ("First", "to new features"),
            ]
        )
        + '<p style="font-size:13px;color:'
        + theme.INK_DIM
        + ';">'
        "Manage your plan any time from Settings.</p>"
    )
    html, text = render_email(
        "You're Pro now.",
        body,
        ("Open your vault", f"{app_url()}/app/vault"),
        preheader="Your Pro subscription is active — everything is unlocked.",
        eyebrow="Loupe Pro",
    )
    return EmailContent("Welcome to Loupe Pro", html, text)


def build_pro_canceled(user: User) -> EmailContent:
    from app.services.email_templates.base import panel

    body = (
        _membership_card(user, active=False)
        + "<p>Your <strong>Loupe Pro</strong> subscription has ended and your "
        "account is back on the free plan.</p>"
        + panel(
            [
                ("Your vault", "Saved — nothing is deleted"),
                ("Price history", "Keeps recording"),
                ("Restart", "One tap in Settings"),
            ]
        )
        + "<p>Pro features simply pause until you're back. Your card is "
        "waiting.</p>"
    )
    html, text = render_email(
        "Pro is paused.",
        body,
        ("Resubscribe in Settings", f"{app_url()}/app/settings"),
        preheader="Your Pro subscription has ended — your data is untouched.",
        eyebrow="Membership",
        eyebrow_color=theme.INK_DIM,
    )
    return EmailContent("Your Loupe Pro subscription ended", html, text)


__all__ = ["build_pro_activated", "build_pro_canceled"]
