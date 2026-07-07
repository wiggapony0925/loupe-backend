"""HTML + SCSS → PDF renderer for portfolio statements (WeasyPrint path).

This is the modern replacement for the hand-drawn ReportLab builder. The
statement is authored as a Jinja2 template (``templates/statement.html.j2``)
styled by SCSS (``templates/statement.scss``, compiled with libsass and
cached), with the charts injected as server-rendered SVG from
:mod:`app.services.analytics.reports.charts`. WeasyPrint turns the whole thing
into print-quality PDF bytes.

``pdf_builder.render_pdf`` calls :func:`render_statement_pdf` and falls back to
a slim ReportLab cover if anything here raises (or WeasyPrint's native libs are
unavailable), so statement generation can never hard-fail.

Heavy imports (``weasyprint``, ``sass``) are done lazily inside functions so
that merely importing this module never requires Pango to be installed.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.analytics.reports import charts
from app.services.analytics.reports.aggregator import ReportSnapshot
from app.services.analytics.reports.images import GALLERY_HOLDINGS

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SCSS_PATH = _TEMPLATES_DIR / "statement.scss"


# ─── Formatting helpers (mirror the ReportLab _money / _pct) ─────────────


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


# ─── Jinja environment (built once) ──────────────────────────────────────


@lru_cache(maxsize=1)
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = _money
    env.filters["pct"] = _pct
    return env


# ─── SCSS → CSS (compiled + cached, recompiled if the file changes) ──────

_css_cache: dict[float, str] = {}


def _compiled_css() -> str:
    """Compile ``statement.scss`` to CSS, cached on the source file's mtime."""
    import sass  # lazy: libsass is a C-ext we only need at render time

    mtime = _SCSS_PATH.stat().st_mtime
    cached = _css_cache.get(mtime)
    if cached is not None:
        return cached
    css = sass.compile(
        string=_SCSS_PATH.read_text(encoding="utf-8"),
        output_style="compressed",
    )
    _css_cache.clear()
    _css_cache[mtime] = css
    return css


# ─── Card art → data URI (so WeasyPrint embeds without a network fetch) ──


def _data_uri(data: bytes) -> str | None:
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    else:
        mime = "image/png"  # WeasyPrint decodes via Pillow regardless
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _image_map(snap: ReportSnapshot) -> dict[str, str]:
    out: dict[str, str] = {}
    for card_id, raw in snap.images.items():
        uri = _data_uri(raw)
        if uri:
            out[card_id] = uri
    return out


# ─── View-model + render ─────────────────────────────────────────────────


def _build_context(snap: ReportSnapshot) -> dict:
    up = snap.delta_usd >= 0
    now = datetime.now(UTC)
    img = _image_map(snap)

    # Allocation legend + donut (only meaningful when a game carries value).
    alloc_total = sum(v for _, v in snap.tcg_breakdown)
    legend: list[dict] = []
    donut_svg = ""
    if snap.tcg_breakdown and alloc_total > 0:
        for i, (name, value) in enumerate(snap.tcg_breakdown):
            legend.append(
                {
                    "name": name.title(),
                    "value": value,
                    "pct": f"{value / alloc_total * 100:.1f}%",
                    "color": charts.ALLOCATION_PALETTE[
                        i % len(charts.ALLOCATION_PALETTE)
                    ],
                }
            )
        donut_svg = charts.donut_chart(snap.tcg_breakdown, alloc_total)

    # Showcase gallery: top holdings that actually have hydrated artwork.
    gallery = [
        {
            "name": h.name,
            "grade": h.grade,
            "value_close_usd": h.value_close_usd,
            "delta_pct": h.delta_pct,
            "art": img[h.card_id],
        }
        for h in snap.holdings[:GALLERY_HOLDINGS]
        if h.card_id in img
    ]

    if snap.unrealized_pnl_usd is None:
        pnl_class = ""
    else:
        pnl_class = "up" if snap.unrealized_pnl_usd >= 0 else "down"

    return {
        # Period / identity
        "period_label": snap.period_label,
        "period_start_short": snap.period_start.strftime("%b %d"),
        "period_start_long": snap.period_start.strftime("%b %d, %Y"),
        "period_end_long": snap.period_end.strftime("%b %d, %Y"),
        "statement_date": now.strftime("%B %d, %Y"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "user_email": snap.user_email,
        # Headline numbers
        "card_count": snap.card_count,
        "opening_value_usd": snap.opening_value_usd,
        "closing_value_usd": snap.closing_value_usd,
        "delta_usd": snap.delta_usd,
        "delta_pct": snap.delta_pct,
        "total_cost_usd": snap.total_cost_usd,
        "unrealized_pnl_usd": snap.unrealized_pnl_usd,
        "unrealized_pnl_pct": snap.unrealized_pnl_pct,
        "avg_grade": snap.avg_grade,
        "up": up,
        "pnl_class": pnl_class,
        # Charts
        "area_svg": charts.area_chart(snap.series, up=up)
        if len(snap.series) >= 2
        else "",
        "donut_svg": donut_svg,
        "bar_svg": charts.bar_chart(snap.grade_buckets),
        "legend": legend,
        # Tables / gallery
        "top_gainers": snap.top_gainers,
        "top_losers": snap.top_losers,
        "gallery": gallery,
        "holdings": snap.holdings,
        # Helper the movers macro calls to look up embedded art.
        "thumb": img.get,
    }


def render_statement_pdf(snap: ReportSnapshot) -> bytes:
    """Render the statement to PDF bytes via Jinja2 + SCSS + WeasyPrint."""
    from weasyprint import CSS, HTML  # lazy: needs Pango at import time

    html = _env().get_template("statement.html.j2").render(**_build_context(snap))
    pdf = HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf(
        stylesheets=[CSS(string=_compiled_css())]
    )
    # Defensive: WeasyPrint always returns bytes for the default (no target)
    # call, but the type stub is Optional — normalize so callers get bytes.
    if pdf is None:  # pragma: no cover
        raise RuntimeError("WeasyPrint returned no PDF bytes")
    return pdf


def scss_source_mtime() -> float:  # small hook for tests / cache-busting
    return os.path.getmtime(_SCSS_PATH)


__all__ = ["render_statement_pdf", "scss_source_mtime"]
