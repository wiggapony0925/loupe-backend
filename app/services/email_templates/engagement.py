"""Engagement templates: the free-plan ceiling, the digest, the milestone.

Where the other modules react to something the *account* did, these react to
something the *collection* did — it filled up, it moved, it finished a set.
Each one leads with the collector's own numbers rather than a pitch, because
the numbers are the reason to open the app.

The digest is recurring non-transactional mail, so it carries the same
one-click unsubscribe treatment as announcements (see ``unsubscribe_footer``);
the cap notice and the milestone are one-shot and transactional.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.user import User
from app.services.email_templates import theme
from app.services.email_templates.announcements import unsubscribe_footer
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    bar_chart,
    big_quote,
    callout,
    card_frame,
    check_list,
    chip,
    display_name,
    esc,
    panel,
    progress_steps,
    render_email,
    stat_tiles,
    usd,
)


def _vault_url() -> str:
    return f"{app_url()}/app/vault"


def _capacity_meter(used: int, limit: int) -> str:
    """A 10-segment fuel gauge for the free-plan vault — full reads as full."""
    filled = min(10, round(10 * used / limit)) if limit else 10
    cells = "".join(
        f'<td style="padding:0 2px;"><div style="height:10px;border-radius:3px;'
        f"background:{theme.ROSE if i < filled else theme.SUNKEN_BG};"
        f'font-size:0;line-height:0;">&nbsp;</div></td>'
        for i in range(10)
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:18px 0 6px;"><tr>{cells}</tr>'
        f'<tr><td colspan="10" align="center" style="padding:10px 2px 0;'
        f"font-size:11px;font-weight:700;letter-spacing:0.08em;"
        f"text-transform:uppercase;color:{theme.INK_DIM};"
        f'font-family:{theme.FONT};">{used} of {limit} cards used</td></tr></table>'
    )


def build_free_limit_reached(
    user: User, *, card_count: int, limit: int
) -> EmailContent:
    """Sent once when a free vault hits its ceiling.

    The collector just tried to add a card and couldn't — so this explains the
    wall, confirms nothing was lost, and offers the one thing that removes it.
    """
    body = (
        f"<p>Hi {esc(display_name(user))} — your vault just hit the free plan's "
        f"<strong>{limit}-card</strong> limit.</p>"
        + _capacity_meter(card_count, limit)
        + callout(
            "Every card you've already added is safe and still tracked — new "
            "additions are what's paused.",
            tone="mint",
        )
        + "<p><strong>Loupe Pro</strong> removes the ceiling:</p>"
        + check_list(
            [
                "Unlimited cards in your vault",
                "Full price history on every holding",
                "Monthly portfolio statements",
                "Priority scanning &amp; grading",
            ]
        )
        + f'<p style="font-size:13px;color:{theme.INK_DIM};">Not ready? You can '
        "keep using Loupe exactly as-is — remove a card any time to free up a "
        "slot.</p>"
    )
    html, text = render_email(
        "Your vault is full.",
        body,
        ("See Pro plans", f"{app_url()}/app/settings/billing"),
        preheader=f"You've used all {limit} cards on the free plan.",
        eyebrow="Vault capacity",
        eyebrow_color=theme.ROSE,
    )
    return EmailContent(f"You've reached {limit} cards on Loupe", html, text)


def build_portfolio_digest(
    user: User,
    *,
    period_label: str,
    total_value_usd: Decimal | float,
    delta_pct: float,
    delta_usd: Decimal | float,
    card_count: int,
    unsub_url: str,
    series: list[float] | None = None,
    top_movers: list[tuple[str, float]] | None = None,
) -> EmailContent:
    """The recurring 'here's what your collection did' email.

    A portfolio app earns its open rate on this one: the headline number, the
    shape of the period, and the cards that actually moved. ``top_movers`` is
    ``(card_name, pct_change)`` — already sorted and trimmed by the caller.
    """
    up = delta_pct >= 0
    tone = "mint" if up else "rose"
    color = theme.MINT if up else theme.ROSE
    arrow = "&#9650;" if up else "&#9660;"
    chart = (
        bar_chart(
            series,
            tone=tone,
            start_label=f"start of {period_label}",
            end_label="now",
        )
        if series and len(series) >= 2
        else ""
    )
    movers = ""
    if top_movers:
        rows = [
            (
                name,
                (
                    f'<span style="color:{theme.MINT if pct >= 0 else theme.ROSE};">'
                    f"{pct:+.1f}%</span>"
                ),
            )
            for name, pct in top_movers
        ]
        movers = (
            f'<p style="margin:22px 0 0;font-size:11px;font-weight:700;'
            f"letter-spacing:0.08em;text-transform:uppercase;color:{theme.INK_DIM};"
            f'font-family:{theme.FONT};">Biggest movers</p>' + panel(rows)
        )
    body = (
        big_quote(usd(total_value_usd), f"Your collection · {period_label}")
        + f'<p style="margin:0 0 8px;text-align:center;">'
        f"{chip(f'{arrow} {delta_pct:+.1f}% ({usd(abs(Decimal(str(delta_usd))))})', tone=tone)}"
        f"</p>"
        + chart
        + stat_tiles(
            [
                (usd(total_value_usd), "total value"),
                (
                    f'<span style="color:{color};">{delta_pct:+.1f}%</span>',
                    period_label.lower(),
                ),
                (str(card_count), "cards tracked"),
            ]
        )
        + movers
        + f'<p style="font-size:13px;color:{theme.INK_DIM};margin-top:18px;">'
        "Prices refresh daily from the markets your cards actually trade on.</p>"
    )
    html, text = render_email(
        f"Your collection, {period_label.lower()}.",
        body,
        ("Open your vault", _vault_url()),
        preheader=f"{usd(total_value_usd)} · {delta_pct:+.1f}% {period_label.lower()}",
        eyebrow=f"{period_label} digest",
        eyebrow_color=color,
        footer_html=unsubscribe_footer(unsub_url),
    )
    return EmailContent(
        f"Your collection is {usd(total_value_usd)} ({delta_pct:+.1f}%)", html, text
    )


def build_set_completed(
    user: User,
    *,
    set_name: str,
    set_total: int,
    series_name: str | None = None,
    set_id: str | None = None,
    image_url: str | None = None,
    total_value_usd: Decimal | float | None = None,
) -> EmailContent:
    """The milestone: every card in a set, owned.

    Set completion is the collecting goal the whole checklist feature exists
    to serve, so this email is a trophy — art, a filled progress bar, and the
    finished count in display type.
    """
    where = f" · {series_name}" if series_name else ""
    art = card_frame(image_url, f"{set_name} set") if image_url else ""
    tiles = [(str(set_total), "cards collected"), ("100%", "complete")]
    if total_value_usd is not None:
        tiles.append((usd(total_value_usd), "set value"))
    body = (
        art
        + big_quote(esc(set_name), f"Complete{where}", color=theme.MINT)
        + f'<p style="margin:0 0 8px;text-align:center;">'
        f"{chip('Set complete', tone='mint')}"
        f"&nbsp;{chip(f'{set_total} of {set_total}', tone='neutral')}</p>"
        + progress_steps(["Started", "Halfway", "Complete"], 2)
        + f"<p>Hi {esc(display_name(user))} — you just finished "
        f"<strong>{esc(set_name)}</strong>. Every single card, in your vault, "
        "tracked at market.</p>"
        + stat_tiles(tiles)
        + callout(
            "Master sets, alternate arts, and graded copies all count "
            "separately — there may still be chase cards worth hunting.",
            tone="mint",
        )
    )
    cta_url = f"{app_url()}/sets/{set_id}" if set_id else _vault_url()
    html, text = render_email(
        "Set complete.",
        body,
        ("See the set", cta_url),
        preheader=f"You've completed {set_name} — all {set_total} cards.",
        eyebrow="Milestone",
    )
    return EmailContent(f"You completed {set_name}", html, text)


__all__ = [
    "build_free_limit_reached",
    "build_portfolio_digest",
    "build_set_completed",
]
