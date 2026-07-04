"""Event notifications the user asked for: price alerts, statement-ready.

The price alert is a stock-quote moment — oversized price, direction arrow,
tinted by move. The statement email leads with the period in display serif,
like the statement's own cover page.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models.user import User
from app.services.email_templates import theme
from app.services.email_templates.base import (
    EmailContent,
    app_url,
    bar_chart,
    big_quote,
    card_frame,
    chip,
    display_name,
    esc,
    panel,
    render_email,
    stat_tiles,
    usd,
)


def build_price_alert(
    *,
    card_name: str,
    set_name: str | None,
    condition: str,
    threshold_usd: Decimal,
    price_usd: Decimal,
    card_id: Any,
    image_url: str | None = None,
    history: list[float] | None = None,
) -> EmailContent:
    """The card-detail page, in an email: the actual card art on its vault
    mount, the quote in display type, the recent price history as a chart,
    then the numbers."""
    where = f" ({esc(set_name)})" if set_name else ""
    up = condition == "above"
    direction = "climbed above" if up else "dropped below"
    arrow = "&#9650;" if up else "&#9660;"
    tone_color = theme.MINT if up else theme.ROSE
    tone = "mint" if up else "rose"
    caption = card_name + (f" · {set_name}" if set_name else "")
    move_pct = (
        (float(price_usd) - float(threshold_usd)) / float(threshold_usd) * 100
        if threshold_usd
        else 0.0
    )
    art = card_frame(image_url, card_name) if image_url else ""
    chart = (
        bar_chart(
            history,
            tone=tone,
            start_label=usd(history[0]),
            end_label=f"now {usd(history[-1])}",
        )
        if history and len(history) >= 2
        else ""
    )
    body = (
        art
        + big_quote(f"{arrow}&nbsp;{usd(price_usd)}", caption, color=tone_color)
        + f'<p style="margin:0 0 8px;text-align:center;">'
        f"{chip(f'{move_pct:+.1f}% vs your alert', tone=tone)}</p>"
        + chart
        + f"<p><strong>{esc(card_name)}</strong>{where} just {direction} your "
        f"<strong>{usd(threshold_usd)}</strong> alert.</p>"
        + panel(
            [
                ("Your alert", f"{'Above' if up else 'Below'} {usd(threshold_usd)}"),
                ("Market price", usd(price_usd)),
            ]
        )
        + f'<p style="font-size:13px;color:{theme.INK_DIM};">Alerts fire '
        "once; set a new one from the card page if you want to keep watching "
        "this card.</p>"
    )
    html, text = render_email(
        "Your alert just fired.",
        body,
        ("View the card", f"{app_url()}/cards/{card_id}"),
        preheader=f"{card_name} is now {usd(price_usd)}.",
        eyebrow="Price alert",
        eyebrow_color=tone_color,
    )
    return EmailContent(f"{card_name} is now {usd(price_usd)}", html, text)


def build_statement_ready(
    user: User,
    *,
    title: str,
    total_value_usd: float | None = None,
    delta_pct: float | None = None,
    card_count: int | None = None,
    series: list[float] | None = None,
) -> EmailContent:
    """Statement notice with the period's real numbers when the caller has
    them: portfolio value chart + closing value / change / holdings tiles.
    Every stat is optional — the email degrades gracefully to the period
    cover when a portfolio was empty."""
    period = title.removesuffix(" statement")
    up = (delta_pct or 0) >= 0
    tone = "mint" if up else "rose"
    chart = (
        bar_chart(
            series,
            tone=tone,
            start_label=f"start of {period}",
            end_label=f"end of {period}",
        )
        if series and len(series) >= 2
        else ""
    )
    tiles = []
    if total_value_usd is not None:
        tiles.append((usd(total_value_usd), "closing value"))
    if delta_pct is not None:
        color = theme.MINT if up else theme.ROSE
        tiles.append(
            (f'<span style="color:{color};">{delta_pct:+.1f}%</span>', "this period")
        )
    if card_count is not None:
        tiles.append((str(card_count), "graded cards"))
    stats = stat_tiles(tiles) if tiles else ""
    body = (
        big_quote(esc(period), "Portfolio statement")
        + f'<p style="margin:0 0 12px;text-align:center;">{chip("PDF", tone="dark")}'
        f"&nbsp;{chip('Kept forever', tone='neutral')}</p>"
        + chart
        + stats
        + f"<p>Hi {esc(display_name(user))} — your <strong>{esc(title)}</strong> "
        "is ready: a full snapshot of your portfolio for the period. Value, "
        "movers, and every holding, archived like a brokerage statement.</p>"
    )
    html, text = render_email(
        "Your statement is in.",
        body,
        ("View your statements", f"{app_url()}/app/statements"),
        preheader=f"Your {title} is ready to view.",
        eyebrow="Statement",
    )
    return EmailContent(f"Your Loupe {title} is ready", html, text)


__all__ = ["build_price_alert", "build_statement_ready"]
