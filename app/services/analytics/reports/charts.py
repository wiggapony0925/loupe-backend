"""Server-rendered SVG charts for the HTML portfolio statement.

WeasyPrint has no JavaScript runtime, so we can't reach for Chart.js / D3.
Instead we emit **inline SVG** — which WeasyPrint rasterises crisply into the
PDF at print resolution — computed from the exact same numbers the ReportLab
builder used. Every function is pure (data in → SVG string out), returns a
``viewBox``-based SVG so CSS can scale it to the column width, and salts its
gradient/clip ``id``s so several charts can coexist on one page without
colliding.

Palette constants mirror ``templates/statement.scss`` (which in turn mirrors
``loupe-web/src/styles/tokens.scss``). Keep the two in sync when tuning brand
colors.
"""

from __future__ import annotations

import itertools
from datetime import date
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle with the aggregator
    from app.services.analytics.reports.aggregator import SeriesPoint

# ─── Brand palette (mirrors statement.scss / frontend tokens) ────────────
INK = "#0E1117"
INK_DIM = "#5B6470"
INK_MUTED = "#9BA3AD"
LINE = "#E5E8EC"
BG_ELEV = "#F7F8FA"
MINT = "#16C09C"
MINT_DARK = "#0E8A6C"
ROSE = "#E5484D"
AMBER = "#F5A524"
BLUE = "#2F6CFB"
VIOLET = "#8B5CF6"

# Allocation palette (donut slices + legend swatches), in draw order.
ALLOCATION_PALETTE = [MINT, BLUE, AMBER, ROSE, VIOLET, INK_DIM, INK_MUTED]

# Process-wide salt so repeated renders never reuse an SVG id within a doc.
_uid_counter = itertools.count(1)


def _uid(prefix: str) -> str:
    return f"{prefix}{next(_uid_counter)}"


def _compact_money(v: float) -> str:
    """Axis-friendly dollar label: $1.2k, $980, $3.4M."""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}${a / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}${a / 1_000:.1f}k"
    return f"{sign}${a:,.0f}"


def _money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _fmt(n: float) -> str:
    """Trim trailing zeros so coordinate strings stay small."""
    return f"{n:.2f}".rstrip("0").rstrip(".")


# ─── Performance: filled area + value line ───────────────────────────────


def area_chart(series: list[SeriesPoint], *, up: bool) -> str:
    """Robinhood-style value curve: soft gradient fill under a colored line,
    dollar-labeled gridlines, and open/close endpoint callouts."""
    if len(series) < 2:
        return ""

    W, H = 720.0, 300.0
    pad_l, pad_r, pad_t, pad_b = 62.0, 20.0, 26.0, 34.0
    pw = W - pad_l - pad_r
    ph = H - pad_t - pad_b

    values = [p.value_usd for p in series]
    lo, hi = min(values), max(values)
    if hi <= lo:  # flat series — pad so the line sits mid-chart
        hi = lo + max(1.0, abs(lo) * 0.05)
    span = hi - lo
    lo -= span * 0.10
    hi += span * 0.14
    rng = hi - lo or 1.0

    tone = MINT if up else ROSE
    n = len(values)

    def sx(i: int) -> float:
        return pad_l + (i / (n - 1)) * pw

    def sy(v: float) -> float:
        return pad_t + (1 - (v - lo) / rng) * ph

    pts = [(sx(i), sy(v)) for i, v in enumerate(values)]
    line_d = "M" + " L".join(f"{_fmt(x)} {_fmt(y)}" for x, y in pts)
    area_d = (
        f"M{_fmt(pts[0][0])} {_fmt(pad_t + ph)} "
        f"L{' L'.join(f'{_fmt(x)} {_fmt(y)}' for x, y in pts)} "
        f"L{_fmt(pts[-1][0])} {_fmt(pad_t + ph)} Z"
    )

    grad = _uid("perfGrad")
    parts: list[str] = [
        f'<svg viewBox="0 0 {int(W)} {int(H)}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Liberation Sans, Arial, sans-serif" role="img">',
        f'<defs><linearGradient id="{grad}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{tone}" stop-opacity="0.22"/>'
        f'<stop offset="1" stop-color="{tone}" stop-opacity="0"/>'
        f"</linearGradient></defs>",
    ]

    # Horizontal gridlines + $ labels (5 bands).
    for k in range(5):
        gy = pad_t + (k / 4) * ph
        gv = hi - (k / 4) * rng
        parts.append(
            f'<line x1="{_fmt(pad_l)}" y1="{_fmt(gy)}" x2="{_fmt(pad_l + pw)}" '
            f'y2="{_fmt(gy)}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_fmt(pad_l - 8)}" y="{_fmt(gy + 3)}" text-anchor="end" '
            f'font-size="12" fill="{INK_MUTED}">{escape(_compact_money(gv))}</text>'
        )

    parts.append(f'<path d="{area_d}" fill="url(#{grad})"/>')
    parts.append(
        f'<path d="{line_d}" fill="none" stroke="{tone}" stroke-width="2.4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    # Endpoint dots.
    for (x, y), col in ((pts[0], INK_MUTED), (pts[-1], tone)):
        parts.append(f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="3.4" fill="{col}"/>')

    # Open / close value callouts above the endpoints.
    parts.append(
        f'<text x="{_fmt(pts[0][0])}" y="{_fmt(max(14, pts[0][1] - 9))}" '
        f'font-size="12.5" font-weight="700" fill="{INK_DIM}">'
        f"{escape(_money(values[0]))}</text>"
    )
    parts.append(
        f'<text x="{_fmt(pts[-1][0])}" y="{_fmt(max(14, pts[-1][1] - 9))}" '
        f'text-anchor="end" font-size="12.5" font-weight="700" fill="{tone}">'
        f"{escape(_money(values[-1]))}</text>"
    )

    # Date axis.
    parts.append(
        f'<text x="{_fmt(pad_l)}" y="{_fmt(H - 10)}" font-size="12" '
        f'fill="{INK_MUTED}">{escape(_axis_date(series[0].date))}</text>'
    )
    parts.append(
        f'<text x="{_fmt(pad_l + pw)}" y="{_fmt(H - 10)}" text-anchor="end" '
        f'font-size="12" fill="{INK_MUTED}">'
        f"{escape(_axis_date(series[-1].date))}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _axis_date(d: date) -> str:
    return d.strftime("%b %d, %Y")


# ─── Allocation: donut (legend lives in the HTML) ────────────────────────


def donut_chart(breakdown: list[tuple[str, float]], total: float) -> str:
    """Donut of allocation-by-value with a centered total-value label.

    The legend (swatch · game · value · share) is rendered in HTML alongside
    this SVG for crisp print typography, so we emit only the ring + label.
    """
    values = [v for _, v in breakdown if v > 0]
    if not values:
        return ""

    W = H = 240.0
    cx = cy = W / 2
    r_outer = 96.0
    r_inner = 60.0
    total_val = sum(values) or 1.0

    parts: list[str] = [
        f'<svg viewBox="0 0 {int(W)} {int(H)}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Liberation Sans, Arial, sans-serif" role="img">'
    ]

    # A single slice (100%) can't be drawn as an SVG arc (start == end); draw
    # two concentric circles to form the ring instead.
    if len(values) == 1:
        color = ALLOCATION_PALETTE[0]
        parts.append(
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r_outer)}" '
            f'fill="{color}"/>'
        )
        parts.append(
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r_inner)}" '
            f'fill="white"/>'
        )
    else:
        angle = -90.0  # start at 12 o'clock
        gap_deg = 2.0  # small white gap between slices
        for i, v in enumerate(values):
            sweep = (v / total_val) * 360.0
            color = ALLOCATION_PALETTE[i % len(ALLOCATION_PALETTE)]
            parts.append(
                _donut_slice(
                    cx, cy, r_outer, r_inner, angle, angle + sweep - gap_deg, color
                )
            )
            angle += sweep

    # Center total.
    parts.append(
        f'<text x="{_fmt(cx)}" y="{_fmt(cy - 2)}" text-anchor="middle" '
        f'font-size="19" font-weight="700" fill="{INK}">'
        f"{escape(_money(total))}</text>"
    )
    parts.append(
        f'<text x="{_fmt(cx)}" y="{_fmt(cy + 16)}" text-anchor="middle" '
        f'font-size="10" font-weight="700" letter-spacing="1" '
        f'fill="{INK_MUTED}">TOTAL</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    from math import cos, radians, sin

    a = radians(deg)
    return cx + r * cos(a), cy + r * sin(a)


def _donut_slice(
    cx: float,
    cy: float,
    r_out: float,
    r_in: float,
    a0: float,
    a1: float,
    color: str,
) -> str:
    large = 1 if (a1 - a0) % 360 > 180 else 0
    x0o, y0o = _polar(cx, cy, r_out, a0)
    x1o, y1o = _polar(cx, cy, r_out, a1)
    x0i, y0i = _polar(cx, cy, r_in, a1)
    x1i, y1i = _polar(cx, cy, r_in, a0)
    d = (
        f"M{_fmt(x0o)} {_fmt(y0o)} "
        f"A{_fmt(r_out)} {_fmt(r_out)} 0 {large} 1 {_fmt(x1o)} {_fmt(y1o)} "
        f"L{_fmt(x0i)} {_fmt(y0i)} "
        f"A{_fmt(r_in)} {_fmt(r_in)} 0 {large} 0 {_fmt(x1i)} {_fmt(y1i)} Z"
    )
    return f'<path d="{d}" fill="{color}"/>'


# ─── Allocation: grade-distribution bars ─────────────────────────────────


def bar_chart(buckets: list[tuple[str, int]]) -> str:
    """Vertical bars for the grade distribution, with count labels on top."""
    counts = [c for _, c in buckets]
    if not any(c > 0 for c in counts):
        return ""

    W, H = 720.0, 260.0
    pad_l, pad_r, pad_t, pad_b = 20.0, 20.0, 26.0, 34.0
    pw = W - pad_l - pad_r
    ph = H - pad_t - pad_b
    hi = max(counts) or 1

    n = len(buckets)
    slot = pw / n
    bar_w = min(slot * 0.52, 74.0)

    parts: list[str] = [
        f'<svg viewBox="0 0 {int(W)} {int(H)}" width="100%" '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'font-family="Liberation Sans, Arial, sans-serif" role="img">'
    ]
    # Baseline.
    base_y = pad_t + ph
    parts.append(
        f'<line x1="{_fmt(pad_l)}" y1="{_fmt(base_y)}" x2="{_fmt(pad_l + pw)}" '
        f'y2="{_fmt(base_y)}" stroke="{LINE}" stroke-width="1.5"/>'
    )
    rid = _uid("bar")
    for i, (label, count) in enumerate(buckets):
        cx = pad_l + slot * (i + 0.5)
        bh = (count / hi) * ph if hi else 0.0
        x = cx - bar_w / 2
        y = base_y - bh
        if count > 0:
            parts.append(
                f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(bar_w)}" '
                f'height="{_fmt(bh)}" rx="5" fill="{MINT}" clip-path="url(#{rid}{i})"/>'
                # rounded only on top: clip the bottom half of the rounding away
                f'<clipPath id="{rid}{i}"><rect x="{_fmt(x)}" y="{_fmt(y)}" '
                f'width="{_fmt(bar_w)}" height="{_fmt(bh + 6)}"/></clipPath>'
            )
            parts.append(
                f'<text x="{_fmt(cx)}" y="{_fmt(y - 8)}" text-anchor="middle" '
                f'font-size="13" font-weight="700" fill="{INK}">{count}</text>'
            )
        parts.append(
            f'<text x="{_fmt(cx)}" y="{_fmt(base_y + 20)}" text-anchor="middle" '
            f'font-size="12.5" fill="{INK_DIM}">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ─── Tiny per-row sparkline ──────────────────────────────────────────────


def sparkline(values: list[float], *, up: bool | None = None) -> str:
    """A 120×32 trend line with no axes, for inline use inside table rows."""
    if len(values) < 2:
        return ""
    W, H = 120.0, 32.0
    pad = 3.0
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    n = len(values)
    if up is None:
        up = values[-1] >= values[0]
    tone = MINT if up else ROSE

    def sx(i: int) -> float:
        return pad + (i / (n - 1)) * (W - 2 * pad)

    def sy(v: float) -> float:
        return pad + (1 - (v - lo) / rng) * (H - 2 * pad)

    d = "M" + " L".join(f"{_fmt(sx(i))} {_fmt(sy(v))}" for i, v in enumerate(values))
    return (
        f'<svg viewBox="0 0 {int(W)} {int(H)}" width="{int(W)}" height="{int(H)}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f'<path d="{d}" fill="none" stroke="{tone}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


__all__ = [
    "ALLOCATION_PALETTE",
    "area_chart",
    "bar_chart",
    "donut_chart",
    "sparkline",
]
