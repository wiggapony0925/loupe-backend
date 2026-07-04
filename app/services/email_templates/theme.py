"""Email theme — the "CSS" of every Loupe email, in one place.

Mail clients strip ``<style>`` blocks and external stylesheets, so email CSS
must be inlined into every tag. These constants are that stylesheet: change a
value here and every template picks it up on the next render.

Values mirror ``@loupe/tokens`` (the "Precision" design language) — the same
palette and type scale the web and mobile apps use. Emails render on the
LIGHT scheme (white card, ink text) with the app's OLED-dark chrome as the
header band, exactly like the marketing site: dark shell, bright content.
"""

from __future__ import annotations

# ── Palette — @loupe/tokens lightColors ───────────────────────────────────
INK = "#0b0b0d"  # ink.default — primary text
INK_MUTED = "#48484a"  # ink.muted — body/secondary text
INK_DIM = "#8e8e93"  # ink.dim — fine print, labels
LINE = "#e5e5ea"  # line.default — hairline borders
PAGE_BG = "#f7f7f8"  # bg.base — page behind the card
CARD_BG = "#ffffff"  # bg.elevated — the content card
SUNKEN_BG = "#efeff2"  # bg.sunken — inset panels
MINT = "#00a86e"  # accent.mint (light) — CTA fill, links
ROSE = "#d63b30"  # accent.rose (light) — drops / warnings
ON_ACCENT = "#ffffff"  # text on a mint fill

# The header band uses the app's dark chrome, where the BRIGHT mint reads
# correctly (light-scheme mint muddies on near-black).
BAND_BG = "#0b0b0d"  # darkColors.bg.sunken — OLED shell
BAND_MINT = "#00f59b"  # darkColors.accent.mint — wordmark
BAND_MUTED = "#6e6e73"  # darkColors.ink.dim — tagline

# Back-compat aliases (older templates/footers reference these names).
INK_DIM_LEGACY = INK_DIM
MUTED = BAND_MUTED

# ── Typography — @loupe/tokens typography ─────────────────────────────────
#: Body copy: the app's sans stack (Inter falls back cleanly in email).
FONT = (
    "'Inter',-apple-system,BlinkMacSystemFont,'SF Pro Text','Segoe UI',"
    "Roboto,Helvetica,Arial,sans-serif"
)
#: Display headings: the app's serif stack — Georgia is the email-safe
#: workhorse; Fraunces/New York render where installed.
FONT_SERIF = (
    "'Fraunces','New York','Iowan Old Style','Palatino Linotype',Georgia,"
    "'Times New Roman',serif"
)

# ── Layout ────────────────────────────────────────────────────────────────
CARD_WIDTH = 560  # px — the white content card
CONTENT_WIDTH = 504  # px — card width minus padding (full-bleed images)

# ── Tints (10-ish% accent on white — callout backgrounds) ─────────────────
MINT_TINT = "#e6f6ef"
ROSE_TINT = "#fdedeb"
AMBER_TINT = "#faf3e0"

#: Mono accents (the dev-portal / terminal flavor).
FONT_MONO = "'SF Mono','JetBrains Mono',ui-monospace,Menlo,monospace"

# ── Reusable inline-style snippets ────────────────────────────────────────
#: The mint pill CTA — white text on mint, like every primary button in the app.
BUTTON_LINK_STYLE = (
    f"display:inline-block;padding:14px 30px;color:{ON_ACCENT};"
    f"text-decoration:none;font-weight:600;font-size:15px;"
    f"letter-spacing:0.01em;font-family:{FONT};"
)
BUTTON_TD_STYLE = f"border-radius:999px;background:{MINT};mso-padding-alt:14px 30px;"

#: Quiet inline link (footer, references).
QUIET_LINK_STYLE = f"color:{MINT};text-decoration:none;font-weight:600;"

#: Small print (unsubscribe lines, legal-ish footers).
FINE_PRINT_STYLE = f"margin:10px 0 0;font-size:12px;line-height:1.6;color:{INK_DIM};"

#: Display heading (the email's h1) — serif, like the app's page titles.
HEADING_STYLE = (
    f"margin:0 0 16px;color:{INK};font-size:27px;line-height:1.25;"
    f"font-weight:600;letter-spacing:-0.01em;font-family:{FONT_SERIF};"
)

#: Tiny uppercase label above the heading — each email family gets its own.
EYEBROW_STYLE = (
    f"margin:0 0 10px;font-size:11px;font-weight:700;letter-spacing:0.18em;"
    f"text-transform:uppercase;font-family:{FONT};"
)

#: Oversized display value (prices, queue positions) — serif, like the app's
#: portfolio total.
BIG_VALUE_STYLE = (
    f"margin:0;font-size:46px;line-height:1.05;font-weight:600;"
    f"letter-spacing:-0.02em;font-family:{FONT_SERIF};"
)

#: Inset key-value panel (quote cards, order-summary style blocks).
PANEL_TABLE_STYLE = (
    f"width:100%;background:{SUNKEN_BG};border:1px solid {LINE};"
    f"border-radius:14px;margin:18px 0 6px;"
)
PANEL_LABEL_STYLE = (
    f"padding:10px 16px;font-size:12px;font-weight:600;letter-spacing:0.06em;"
    f"text-transform:uppercase;color:{INK_DIM};font-family:{FONT};"
    f"text-align:left;white-space:nowrap;"
)
PANEL_VALUE_STYLE = (
    f"padding:10px 16px;font-size:14px;font-weight:600;color:{INK};"
    f"font-family:{FONT};text-align:right;"
)

__all__ = [
    "AMBER_TINT",
    "BAND_BG",
    "BAND_MINT",
    "BAND_MUTED",
    "BIG_VALUE_STYLE",
    "BUTTON_LINK_STYLE",
    "BUTTON_TD_STYLE",
    "CARD_BG",
    "CARD_WIDTH",
    "CONTENT_WIDTH",
    "EYEBROW_STYLE",
    "FINE_PRINT_STYLE",
    "FONT",
    "FONT_MONO",
    "FONT_SERIF",
    "HEADING_STYLE",
    "INK",
    "INK_DIM",
    "INK_MUTED",
    "LINE",
    "MINT",
    "MINT_TINT",
    "MUTED",
    "ON_ACCENT",
    "PAGE_BG",
    "PANEL_LABEL_STYLE",
    "PANEL_TABLE_STYLE",
    "PANEL_VALUE_STYLE",
    "ROSE",
    "ROSE_TINT",
    "SUNKEN_BG",
]
