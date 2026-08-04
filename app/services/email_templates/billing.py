"""Billing templates: Pro activation, cancellation, payment trouble, expiry.

Pro activation is the celebration email — it renders a dark membership card
(the app's OLED chrome with the bright-mint PRO wordmark). Every other billing
email reuses that same card in a different state, so the card itself carries
the status at a glance: PAUSED when it lapses, ACTION NEEDED when a payment
fails, ENDS SOON when a cancellation is scheduled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
    render_email,
    stat_tiles,
    usd,
)

#: Bright amber that survives the dark membership card (the light-scheme
#: amber muddies on near-black, same reason the mint has a BAND variant).
_BAND_AMBER = "#ffc857"

#: Membership-card states → (bg, wordmark, name, meta, badge bg/ink/label).
_CARD_STATES = {
    "active": (
        theme.BAND_BG,
        theme.BAND_MINT,
        "#f5f5f7",
        theme.BAND_MUTED,
        "#12291e",
        theme.BAND_MINT,
        "ACTIVE",
    ),
    "paused": (
        theme.SUNKEN_BG,
        theme.INK_DIM,
        theme.INK_MUTED,
        theme.INK_DIM,
        "#e2e2e6",
        theme.INK_MUTED,
        "PAUSED",
    ),
    "at_risk": (
        theme.BAND_BG,
        _BAND_AMBER,
        "#f5f5f7",
        theme.BAND_MUTED,
        "#33280d",
        _BAND_AMBER,
        "ACTION NEEDED",
    ),
    "ending": (
        theme.BAND_BG,
        theme.BAND_MUTED,
        "#f5f5f7",
        theme.BAND_MUTED,
        "#2a2a2e",
        theme.BAND_MUTED,
        "ENDS SOON",
    ),
}


def _member_since(user: User) -> int:
    stamp = getattr(user, "pro_since", None) or getattr(user, "created_at", None)
    return stamp.year if stamp else datetime.now(UTC).year


def _membership_card(user: User, *, state: str = "active") -> str:
    """The Pro 'card', rendered in one of :data:`_CARD_STATES`."""
    bg, word, name_c, meta_c, badge_bg, badge_c, badge_txt = _CARD_STATES[state]
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
        _membership_card(user, state="active")
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
    body = (
        _membership_card(user, state="paused")
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


def _billing_url() -> str:
    return f"{app_url()}/app/settings/billing"


def build_payment_failed(
    user: User,
    *,
    amount_usd: Decimal | float | None = None,
    attempt: int = 1,
    max_attempts: int = 4,
    next_attempt: datetime | None = None,
    grace_days: int | None = None,
) -> EmailContent:
    """Dunning: a charge failed and Pro is on the clock.

    This is the one billing email a user must not miss — without it a lapsed
    card silently downgrades them and the first they hear of it is a locked
    feature. Leads with what broke, then exactly how long they have.
    """
    retry = (
        f"We'll retry automatically on "
        f"{next_attempt.astimezone(UTC).strftime('%-d %b')}."
        if next_attempt
        else "We'll retry automatically over the next few days."
    )
    rows = [("Status", "Payment declined")]
    if amount_usd is not None:
        rows.append(("Amount due", usd(amount_usd)))
    rows.append(("Attempt", f"{attempt} of {max_attempts}"))
    if next_attempt is not None:
        rows.append(
            ("Next retry", esc(next_attempt.astimezone(UTC).strftime("%-d %b %Y")))
        )
    body = (
        _membership_card(user, state="at_risk")
        + f"<p>Hi {esc(display_name(user))} — we couldn't charge your card for "
        "<strong>Loupe Pro</strong>. Your Pro features are still on for now.</p>"
        + panel(rows)
        + callout(
            f"{retry} Updating your card fixes this instantly — no need to "
            "resubscribe.",
            tone="amber",
        )
        + (
            f'<p style="margin:14px 0 4px;text-align:center;">'
            f"{chip(f'Pro stays on for {grace_days} more days', tone='amber')}</p>"
            if grace_days is not None
            else ""
        )
        + f'<p style="font-size:13px;color:{theme.INK_DIM};">Most declines are '
        "an expired card or a bank hold — both take a minute to fix. If the "
        "retries run out, your account moves to the free plan and your vault "
        "stays exactly where it is.</p>"
    )
    html, text = render_email(
        "Your payment didn't go through.",
        body,
        ("Update your card", _billing_url()),
        preheader="A card update keeps your Pro features on.",
        eyebrow="Action needed",
        eyebrow_color="#b8860b",
    )
    return EmailContent("Your Loupe Pro payment failed", html, text)


def build_pro_expiring(
    user: User, *, ends_on: datetime, days_left: int
) -> EmailContent:
    """The heads-up before a scheduled cancellation actually lands.

    Sent for ``cancel_at_period_end`` subscriptions — the user already chose
    to cancel, so this is a factual reminder with an easy undo, not a pitch.
    """
    when = ends_on.astimezone(UTC).strftime("%-d %B %Y")
    window = f"{days_left} day{'s' if days_left != 1 else ''}"
    body = (
        _membership_card(user, state="ending")
        + f"<p>Hi {esc(display_name(user))} — your <strong>Loupe Pro</strong> "
        f"membership is set to end on <strong>{esc(when)}</strong>. Until then "
        "everything stays unlocked.</p>"
        + f'<p style="margin:14px 0 4px;text-align:center;">'
        f"{chip(f'{window} left', tone='amber')}"
        f"&nbsp;{chip('Cancel anytime', tone='neutral')}</p>"
        + panel(
            [
                ("Pro ends", esc(when)),
                ("Your vault", "Kept — nothing is deleted"),
                ("After that", "Free plan"),
            ]
        )
        + callout(
            "Changed your mind? Resuming before that date keeps everything "
            "running without a gap.",
            tone="mint",
        )
    )
    html, text = render_email(
        "Your Pro membership ends soon.",
        body,
        ("Keep Pro on", _billing_url()),
        preheader=f"Loupe Pro ends on {when} — {window} left.",
        eyebrow="Membership",
        eyebrow_color=theme.INK_DIM,
    )
    return EmailContent(f"Your Loupe Pro membership ends in {window}", html, text)


__all__ = [
    "build_payment_failed",
    "build_pro_activated",
    "build_pro_canceled",
    "build_pro_expiring",
]
