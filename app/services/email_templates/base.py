"""The email skeleton: one renderer every template goes through.

``render_email`` produces the full HTML document (Outlook-safe tables, hidden
preheader, dark-mode meta) AND the plain-text part in one call, so no
template can ship without deliverability basics. Templates never touch raw
HTML structure — they compose body fragments and hand them here.
"""

from __future__ import annotations

import html as html_mod
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

from app.config import get_settings
from app.models.user import User
from app.services.email_templates import theme


class EmailContent(NamedTuple):
    """A fully rendered email: what the transport (and previews) consume."""

    subject: str
    html: str
    text: str


def app_url() -> str:
    """Public web-app origin for links in email bodies (no trailing slash)."""
    return get_settings().app_public_url.rstrip("/")


def esc(value: object) -> str:
    """HTML-escape user-supplied content before it lands in a template."""
    return html_mod.escape(str(value), quote=True)


def display_name(user: User) -> str:
    return user.display_name or (user.email or "there").split("@", 1)[0]


def usd(amount: Decimal | float | str) -> str:
    return f"${Decimal(str(amount)):,.2f}"


def paragraphs(body_text: str) -> str:
    """Plain text → escaped paragraph HTML. Blank lines split paragraphs,
    single newlines become <br>. No markup passes through — even admin-only
    composers never carry raw HTML."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", body_text) if p.strip()]
    return "".join(f"<p>{esc(p).replace(chr(10), '<br>')}</p>" for p in parts)


def panel(rows: list[tuple[str, str]]) -> str:
    """An inset key-value card (order-summary style): label left, value right.

    Values are pre-escaped by the caller when user-supplied — this lets
    templates pass styled fragments (a mint price, a badge) as values.
    """
    body = "".join(
        f"<tr>"
        f'<td style="{theme.PANEL_LABEL_STYLE}{"" if i else "padding-top:16px;"}'
        f'{"padding-bottom:16px;" if i == len(rows) - 1 else ""}">{esc(label)}</td>'
        f'<td style="{theme.PANEL_VALUE_STYLE}{"" if i else "padding-top:16px;"}'
        f'{"padding-bottom:16px;" if i == len(rows) - 1 else ""}">{value}</td>'
        f"</tr>"
        for i, (label, value) in enumerate(rows)
    )
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'style="{theme.PANEL_TABLE_STYLE}">{body}</table>'
    )


def callout(inner_html: str, *, tone: str = "mint") -> str:
    """A tinted rounded box — the email equivalent of the app's NoteCard.

    Tones: ``mint`` (good news / confirmations), ``rose`` (warnings /
    "wasn't you?"), ``amber`` (heads-up), ``neutral`` (plain inset).
    """
    bg, border = {
        "mint": (theme.MINT_TINT, theme.MINT),
        "rose": (theme.ROSE_TINT, theme.ROSE),
        "amber": (theme.AMBER_TINT, "#b8860b"),
        "neutral": (theme.SUNKEN_BG, theme.LINE),
    }[tone]
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:16px 0 6px;"><tr>'
        f'<td style="background:{bg};border-left:3px solid {border};'
        f"border-radius:10px;padding:14px 18px;color:{theme.INK};font-size:14px;"
        f'line-height:1.6;font-family:{theme.FONT};">{inner_html}</td>'
        f"</tr></table>"
    )


def chip(text: str, *, tone: str = "mint") -> str:
    """Small inline status pill (ACTIVE, INTERVIEW, EXPIRES IN 30 MIN…)."""
    bg, color = {
        "mint": (theme.MINT_TINT, theme.MINT),
        "rose": (theme.ROSE_TINT, theme.ROSE),
        "amber": (theme.AMBER_TINT, "#b8860b"),
        "neutral": (theme.SUNKEN_BG, theme.INK_MUTED),
        "dark": (theme.BAND_BG, theme.BAND_MINT),
    }[tone]
    return (
        f'<span style="display:inline-block;padding:4px 12px;border-radius:999px;'
        f"background:{bg};color:{color};font-size:11px;font-weight:700;"
        f'letter-spacing:0.1em;text-transform:uppercase;font-family:{theme.FONT};">'
        f"{esc(text)}</span>"
    )


def big_quote(
    value_html: str, caption: str | None = None, *, color: str | None = None
) -> str:
    """Centered oversized display value — the 'portfolio total' moment."""
    caption_html = (
        f'<p style="margin:8px 0 0;font-size:13px;color:{theme.INK_DIM};'
        f'font-family:{theme.FONT};">{esc(caption)}</p>'
        if caption
        else ""
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:22px 0 10px;"><tr><td align="center">'
        f'<p style="{theme.BIG_VALUE_STYLE}color:{color or theme.INK};">'
        f"{value_html}</p>{caption_html}</td></tr></table>"
    )


def bar_chart(
    values: list[float],
    *,
    tone: str = "mint",
    start_label: str | None = None,
    end_label: str | None = None,
) -> str:
    """A price sparkline built from table cells — no images, no SVG, so it
    renders in every mail client (Gmail strips ``<svg>``). History bars are
    tinted; the latest bar is solid."""
    if not values or len(values) < 2:
        return ""
    solid = theme.MINT if tone == "mint" else theme.ROSE
    faded = theme.MINT_TINT if tone == "mint" else theme.ROSE_TINT
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    cells = []
    for i, v in enumerate(values):
        h = 10 + int(42 * (float(v) - float(lo)) / float(span))
        bg = solid if i == len(values) - 1 else faded
        cells.append(
            f'<td valign="bottom" style="padding:0 2px;">'
            f'<div style="height:{h}px;background:{bg};border-radius:3px 3px 0 0;'
            f'font-size:0;line-height:0;">&nbsp;</div></td>'
        )
    labels = (
        f'<tr><td colspan="{len(values)}" style="padding:6px 2px 0;">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0"><tr>'
        f'<td style="font-size:11px;color:{theme.INK_DIM};'
        f'font-family:{theme.FONT};">{esc(start_label or "")}</td>'
        f'<td align="right" style="font-size:11px;color:{theme.INK_DIM};'
        f'font-family:{theme.FONT};">{esc(end_label or "")}</td>'
        f"</tr></table></td></tr>"
        if (start_label or end_label)
        else ""
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:18px 0 6px;">'
        f"<tr>{''.join(cells)}</tr>"
        f'<tr><td colspan="{len(values)}" style="border-top:2px solid '
        f'{theme.LINE};font-size:0;line-height:0;">&nbsp;</td></tr>'
        f"{labels}</table>"
    )


def card_frame(image_url: str, alt: str, *, width: int = 168) -> str:
    """Card artwork on the app's dark tile mount — the vault look."""
    inner = width - 16
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'align="center" style="margin:4px auto;"><tr>'
        f'<td style="background:{theme.BAND_BG};border-radius:14px;padding:8px;">'
        f'<img src="{esc(image_url)}" alt="{esc(alt)}" width="{inner}" '
        f'style="display:block;width:{inner}px;height:auto;border-radius:8px;">'
        f"</td></tr></table>"
    )


def stat_tiles(tiles: list[tuple[str, str]]) -> str:
    """A 2-per-row grid of stat tiles: big serif value over a dim label."""
    rows = []
    for i in range(0, len(tiles), 2):
        pair = tiles[i : i + 2]
        cells = "".join(
            f'<td width="50%" style="padding:6px;">'
            f'<table role="presentation" width="100%" cellspacing="0" '
            f'cellpadding="0" border="0"><tr><td align="center" '
            f'style="background:{theme.SUNKEN_BG};border:1px solid {theme.LINE};'
            f'border-radius:12px;padding:16px 10px;">'
            f'<p style="margin:0;font-size:24px;font-weight:600;color:{theme.INK};'
            f'letter-spacing:-0.01em;font-family:{theme.FONT_SERIF};">{value}</p>'
            f'<p style="margin:4px 0 0;font-size:11px;font-weight:600;'
            f"letter-spacing:0.08em;text-transform:uppercase;color:{theme.INK_DIM};"
            f'font-family:{theme.FONT};">{esc(label)}</p>'
            f"</td></tr></table></td>"
            for value, label in pair
        )
        if len(pair) == 1:
            cells += '<td width="50%" style="padding:6px;">&nbsp;</td>'
        rows.append(f"<tr>{cells}</tr>")
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:14px 0 6px;">{"".join(rows)}</table>'
    )


def progress_steps(labels: list[str], current: int, *, tone: str = "mint") -> str:
    """A segmented progress bar with step labels (application pipeline,
    security level). Steps ≤ ``current`` are solid; the rest stay sunken."""
    solid = theme.MINT if tone == "mint" else theme.ROSE
    seg_cells = "".join(
        f'<td style="padding:0 3px;"><div style="height:6px;border-radius:3px;'
        f"background:{solid if i <= current else theme.SUNKEN_BG};"
        f'font-size:0;line-height:0;">&nbsp;</div></td>'
        for i in range(len(labels))
    )
    label_cells = "".join(
        f'<td align="center" style="padding:6px 2px 0;font-size:10px;'
        f"font-weight:700;letter-spacing:0.08em;text-transform:uppercase;"
        f"color:{theme.INK if i == current else theme.INK_DIM};"
        f'font-family:{theme.FONT};">{esc(label)}</td>'
        for i, label in enumerate(labels)
    )
    return (
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="margin:16px 0 6px;">'
        f"<tr>{seg_cells}</tr><tr>{label_cells}</tr></table>"
    )


def check_list(items: list[str]) -> str:
    """Feature bullets with the app's mint check — email-safe table rows."""
    rows = "".join(
        f'<tr><td style="width:24px;padding:5px 0;vertical-align:top;'
        f"color:{theme.MINT};font-weight:700;font-family:{theme.FONT};"
        f'font-size:14px;">&#10003;</td>'
        f'<td style="padding:5px 0;color:{theme.INK_MUTED};font-size:15px;'
        f'line-height:1.55;font-family:{theme.FONT};">{item}</td></tr>'
        for item in items
    )
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'style="margin:6px 0;">{rows}</table>'
    )


_TAG_RE = re.compile(r"<[^>]+>")


def _to_text(fragment: str) -> str:
    """Collapse an HTML fragment to readable plain text (for the text part)."""
    # Links must survive as text — "Unsubscribe (https://…)" — or the text
    # part loses the URLs compliance and users depend on.
    text = re.sub(r'(?is)<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"\2 (\1)", fragment)
    text = re.sub(r"(?i)</(p|li|ul|ol|h[1-6]|tr|div)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = html_mod.unescape(_TAG_RE.sub("", text))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def render_email(
    heading: str,
    body_html: str,
    cta: tuple[str, str] | None = None,
    *,
    preheader: str = "",
    footer_html: str = "",
    eyebrow: str | None = None,
    eyebrow_color: str | None = None,
) -> tuple[str, str]:
    """Render a themed Loupe email → ``(html, text)``.

    Dark header band, white content card on a soft page background, mint CTA.
    Table-based layout with inline styles only (Outlook strips ``<style>``
    and doesn't honor CSS box-model on divs). The hidden preheader controls
    the inbox preview line. ``footer_html`` extends the footer (e.g. an
    unsubscribe line on announcement mail).
    """
    t = theme
    base = app_url()
    button = (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" '
        f'style="margin:26px 0 6px;"><tr><td style="{t.BUTTON_TD_STYLE}">'
        f'<a href="{esc(cta[1])}" target="_blank" style="{t.BUTTON_LINK_STYLE}">'
        f"{esc(cta[0])} &rarr;</a></td></tr></table>"
        if cta
        else ""
    )
    # &zwnj;&nbsp; padding stops clients from pulling body text into the
    # preview line after a short preheader.
    preheader_div = (
        f'<div style="display:none;font-size:1px;line-height:1px;max-height:0;'
        f'max-width:0;opacity:0;overflow:hidden;mso-hide:all;">'
        f"{esc(preheader)}{'&zwnj;&nbsp;' * 24}</div>"
        if preheader
        else ""
    )
    year = datetime.now(UTC).year
    html = (
        "<!DOCTYPE html>"
        '<html lang="en" dir="ltr">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{esc(heading)}</title>"
        "</head>"
        f'<body style="margin:0;padding:0;background:{t.PAGE_BG};'
        f'font-family:{t.FONT};-webkit-text-size-adjust:100%;">'
        f"{preheader_div}"
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0" style="background:{t.PAGE_BG};">'
        f'<tr><td align="center" style="padding:32px 16px;">'
        f'<table role="presentation" width="{t.CARD_WIDTH}" cellspacing="0" '
        f'cellpadding="0" border="0" style="max-width:{t.CARD_WIDTH}px;width:100%;'
        f"background:{t.CARD_BG};border:1px solid {t.LINE};border-radius:20px;"
        f'box-shadow:0 1px 2px rgba(16,24,40,0.06),0 8px 24px rgba(16,24,40,0.06);">'
        # Header band — the app's OLED chrome with the bright-mint wordmark.
        f"<tr><td "
        f'style="background:{t.BAND_BG};padding:22px 32px;'
        f'border-radius:20px 20px 0 0;">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        f'border="0"><tr>'
        f'<td style="color:{t.BAND_MINT};font-size:17px;font-weight:800;'
        f'letter-spacing:0.14em;font-family:{t.FONT};">&#9670;&nbsp;LOUPE</td>'
        f'<td align="right" style="color:{t.BAND_MUTED};font-size:11px;'
        f"font-weight:600;letter-spacing:0.18em;"
        f'font-family:{t.FONT};">PORTFOLIO &middot; MARKETS</td>'
        f"</tr></table></td></tr>"
        # Mint accent hairline under the band.
        f'<tr><td style="height:3px;background:{t.MINT};font-size:0;'
        f'line-height:0;">&nbsp;</td></tr>'
        # Content
        f'<tr><td style="padding:34px 32px 10px;color:{t.INK_MUTED};'
        f'line-height:1.65;font-size:15px;font-family:{t.FONT};">'
        + (
            f'<p style="{t.EYEBROW_STYLE}color:{eyebrow_color or t.MINT};">'
            f"{esc(eyebrow)}</p>"
            if eyebrow
            else ""
        )
        + f'<h1 style="{t.HEADING_STYLE}">{heading}</h1>'
        f"{body_html}{button}"
        f"</td></tr>"
        # Footer — sign-off, quick links, then the small print.
        f'<tr><td style="border-top:1px solid {t.LINE};padding:20px 32px 26px;'
        f"color:{t.INK_DIM};font-size:13px;line-height:1.6;font-family:{t.FONT};"
        f'border-radius:0 0 20px 20px;">'
        f'<p style="margin:0 0 4px;font-weight:600;color:{t.INK};">'
        f"&mdash; The Loupe team</p>"
        f'<p style="margin:0 0 12px;">Track every card like a position &mdash; '
        f"on the web and in your pocket.</p>"
        f'<p style="margin:0;font-size:12px;">'
        f'<a href="{base}/app/vault" target="_blank" '
        f'style="{t.QUIET_LINK_STYLE}">Vault</a>'
        f'<span style="color:{t.LINE};">&nbsp;&nbsp;&middot;&nbsp;&nbsp;</span>'
        f'<a href="{base}/app/markets" target="_blank" '
        f'style="{t.QUIET_LINK_STYLE}">Markets</a>'
        f'<span style="color:{t.LINE};">&nbsp;&nbsp;&middot;&nbsp;&nbsp;</span>'
        f'<a href="{base}/app/settings" target="_blank" '
        f'style="{t.QUIET_LINK_STYLE}">Settings</a></p>'
        f'<p style="margin:12px 0 0;font-size:12px;color:{t.INK_DIM};">'
        f"&copy; {year} Loupe &middot; You're receiving this because you have "
        f"a Loupe account.</p>"
        f"{footer_html}"
        f"</td></tr>"
        f"</table></td></tr></table></body></html>"
    )
    text_parts = [_to_text(heading), "", _to_text(body_html)]
    if cta:
        text_parts += ["", f"{cta[0]}: {cta[1]}"]
    text_parts += ["", "— The Loupe team", base]
    if footer_html:
        text_parts += ["", _to_text(footer_html)]
    return html, "\n".join(text_parts).strip() + "\n"


__all__ = [
    "EmailContent",
    "app_url",
    "bar_chart",
    "big_quote",
    "callout",
    "card_frame",
    "check_list",
    "chip",
    "display_name",
    "esc",
    "panel",
    "paragraphs",
    "progress_steps",
    "render_email",
    "stat_tiles",
    "usd",
]
