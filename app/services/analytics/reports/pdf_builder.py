"""PDF renderer for monthly / yearly portfolio statements.

Pure ReportLab Platypus — no external services, no heavy templating
engine. The layout is intentionally Amex-statement-ish:

  • Cover page: brand mark, "Portfolio Statement", period, opening &
    closing value, period P/L chip.
  • Performance page: a value-over-time line chart with axis labels.
  • Allocation page: TCG breakdown + grade distribution bars.
  • Top movers page: gainers + losers tables.
  • Holdings page(s): full sorted vault table, paginated automatically.
  • Footer on every page: generated-at timestamp + page number.

Each section is a free function that takes the snapshot and returns a
list of Platypus flowables. That keeps the builder declarative and
makes it trivial to reuse / reorder sections later (e.g. a "lite"
quarterly statement). Reportlab handles pagination + page breaks for us.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import UTC, datetime

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.analytics.reports.aggregator import ReportSnapshot

# ─── Brand palette (mirrors loupe-frontend theme tokens) ─────────────────
INK = colors.HexColor("#0E1117")
INK_DIM = colors.HexColor("#5B6470")
INK_MUTED = colors.HexColor("#9BA3AD")
LINE = colors.HexColor("#E5E8EC")
BG_ELEV = colors.HexColor("#F7F8FA")
MINT = colors.HexColor("#16C09C")
ROSE = colors.HexColor("#E5484D")
AMBER = colors.HexColor("#F5A524")
BLUE = colors.HexColor("#2F6CFB")

PAGE_W, PAGE_H = LETTER
MARGIN = 0.6 * inch


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


def _eyebrow(text: str, st: dict[str, ParagraphStyle]) -> Paragraph:
    """Small uppercase section kicker, like a statement's section labels."""
    return Paragraph(text.upper(), st["eyebrow"])


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=28,
            leading=32,
            textColor=INK,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=INK,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "eyebrow": ParagraphStyle(
            "eyebrow",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=INK_MUTED,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10,
            leading=14,
            textColor=INK,
        ),
        "dim": ParagraphStyle(
            "dim",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=INK_DIM,
        ),
        "hero": ParagraphStyle(
            "hero",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=40,
            leading=44,
            textColor=INK,
        ),
        "brand": ParagraphStyle(
            "brand",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=18,
            textColor=MINT,
            spaceAfter=2,
        ),
    }


# ─── Section renderers (each returns a list of flowables) ────────────────


def _section_cover(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    up = snap.delta_usd >= 0
    chip_color = MINT if up else ROSE
    chip_text = f"{_money(snap.delta_usd)} ({_pct(snap.delta_pct)})"
    delta_para = Paragraph(
        f'<font color="{chip_color.hexval()}"><b>{chip_text}</b></font> '
        f'<font color="{INK_MUTED.hexval()}">over {snap.period_label}</font>',
        st["body"],
    )
    return [
        Paragraph("◆ LOUPE", st["brand"]),
        Paragraph("Portfolio Statement", st["h1"]),
        Paragraph(snap.period_label, st["dim"]),
        Spacer(1, 0.28 * inch),
        _account_table(snap, st),
        Spacer(1, 0.32 * inch),
        _eyebrow("Closing value", st),
        Paragraph(_money(snap.closing_value_usd), st["hero"]),
        delta_para,
        Spacer(1, 0.32 * inch),
        _eyebrow("Account summary", st),
        Spacer(1, 0.06 * inch),
        _summary_table(snap, st),
    ]


def _account_table(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
    """Two-column account-info strip, the way a brokerage statement opens."""
    statement_date = datetime.now(UTC).strftime("%B %d, %Y")
    period = (
        f"{snap.period_start.strftime('%b %d, %Y')} – "
        f"{snap.period_end.strftime('%b %d, %Y')}"
    )
    data = [
        ["Account holder", snap.user_email, "Statement date", statement_date],
        ["Statement period", period, "Vault holdings", f"{snap.card_count:,} cards"],
    ]
    t = Table(
        data,
        colWidths=[1.25 * inch, 2.0 * inch, 1.2 * inch, 1.45 * inch],
    )
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_DIM),
                ("TEXTCOLOR", (2, 0), (2, -1), INK_DIM),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("TEXTCOLOR", (3, 0), (3, -1), INK),
                ("BACKGROUND", (0, 0), (-1, -1), BG_ELEV),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return t


def _summary_table(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
    data = [
        ["Opening value", _money(snap.opening_value_usd)],
        ["Closing value", _money(snap.closing_value_usd)],
        ["Period change", f"{_money(snap.delta_usd)}  ({_pct(snap.delta_pct)})"],
        ["Cards in vault", f"{snap.card_count:,}"],
        ["Average grade", f"{snap.avg_grade:.2f}" if snap.avg_grade else "—"],
        ["Total cost basis", _money(snap.total_cost_usd)],
        [
            "Unrealized P/L",
            f"{_money(snap.unrealized_pnl_usd)}  ({_pct(snap.unrealized_pnl_pct)})",
        ],
    ]
    t = Table(data, colWidths=[2.4 * inch, 3.5 * inch])
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("TEXTCOLOR", (0, 0), (0, -1), INK_DIM),
                ("TEXTCOLOR", (1, 0), (1, -1), INK),
                ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return t


def _section_performance(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    if len(snap.series) < 2:
        return [
            _eyebrow("Performance", st),
            Paragraph("Performance", st["h2"]),
            Paragraph(
                "Not enough price history was available during this period to "
                "render a chart. As your scanner accumulates daily prices the "
                "next statement will include a full value curve.",
                st["dim"],
            ),
        ]
    return [
        _eyebrow("Performance", st),
        Paragraph("Performance", st["h2"]),
        Paragraph("Daily value across the period.", st["dim"]),
        Spacer(1, 0.15 * inch),
        _line_chart(snap),
    ]


def _line_chart(snap: ReportSnapshot) -> Drawing:
    width = PAGE_W - 2 * MARGIN
    d = Drawing(width, 220)
    data = [[(i, p.value_usd) for i, p in enumerate(snap.series)]]
    lp = LinePlot()
    lp.x = 40
    lp.y = 30
    lp.height = 170
    lp.width = width - 60
    lp.data = data
    lp.lines[0].strokeColor = MINT if snap.delta_usd >= 0 else ROSE
    lp.lines[0].strokeWidth = 1.6
    lp.xValueAxis.visibleLabels = 0
    lp.xValueAxis.visibleTicks = 0
    lp.xValueAxis.strokeColor = LINE
    lp.yValueAxis.strokeColor = LINE
    lp.yValueAxis.gridStrokeColor = LINE
    lp.yValueAxis.gridStrokeWidth = 0.3
    lp.yValueAxis.visibleGrid = 1
    lp.yValueAxis.labels.fontSize = 7
    lp.yValueAxis.labels.fillColor = INK_DIM
    d.add(lp)
    d.add(
        String(
            40,
            10,
            snap.series[0].date.isoformat(),
            fontName="Helvetica",
            fontSize=7,
            fillColor=INK_MUTED,
        )
    )
    d.add(
        String(
            width - 80,
            10,
            snap.series[-1].date.isoformat(),
            fontName="Helvetica",
            fontSize=7,
            fillColor=INK_MUTED,
            textAnchor="end",
        )
    )
    return d


def _section_allocation(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    flow = [_eyebrow("Composition", st), Paragraph("Allocation", st["h2"])]
    # A breakdown is only meaningful when at least one game carries value.
    if not snap.tcg_breakdown or sum(v for _, v in snap.tcg_breakdown) <= 0:
        flow.append(
            Paragraph("No cards in vault yet — nothing to allocate.", st["dim"])
        )
        return flow
    flow.append(Paragraph("By trading-card game (closing value).", st["dim"]))
    flow.append(Spacer(1, 0.15 * inch))
    flow.append(_pie_chart(snap))
    flow.append(Spacer(1, 0.3 * inch))
    flow.append(Paragraph("Grade distribution", st["h2"]))
    # ReportLab's VerticalBarChart throws on empty data — guard it so a vault
    # with no graded buckets degrades to an honest note instead of failing the
    # whole statement.
    if any(count > 0 for _, count in snap.grade_buckets):
        flow.append(_grade_bar_chart(snap))
    else:
        flow.append(
            Paragraph("No graded cards in this period yet.", st["dim"])
        )
    return flow


def _pie_chart(snap: ReportSnapshot) -> Drawing:
    width = PAGE_W - 2 * MARGIN
    d = Drawing(width, 180)
    pie = Pie()
    pie.x = 60
    pie.y = 10
    pie.width = 160
    pie.height = 160
    pie.data = [v for _, v in snap.tcg_breakdown] or [1]
    pie.labels = [k for k, _ in snap.tcg_breakdown]
    palette = [MINT, BLUE, AMBER, ROSE, INK_DIM, INK_MUTED]
    for i, _ in enumerate(snap.tcg_breakdown):
        pie.slices[i].fillColor = palette[i % len(palette)]
        pie.slices[i].strokeColor = colors.white
        pie.slices[i].strokeWidth = 1.2
    # Legend column on the right.
    cursor_y = 150
    for i, (label, value) in enumerate(snap.tcg_breakdown):
        sw = palette[i % len(palette)]
        d.add(String(260, cursor_y, "■", fontSize=14, fillColor=sw))
        d.add(
            String(
                278,
                cursor_y + 1,
                f"{label.title()}",
                fontName="Helvetica-Bold",
                fontSize=10,
                fillColor=INK,
            )
        )
        d.add(
            String(
                380,
                cursor_y + 1,
                _money(value),
                fontName="Helvetica",
                fontSize=10,
                fillColor=INK_DIM,
            )
        )
        cursor_y -= 20
    d.add(pie)
    return d


def _grade_bar_chart(snap: ReportSnapshot) -> Drawing:
    width = PAGE_W - 2 * MARGIN
    d = Drawing(width, 160)
    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 30
    bc.width = width - 80
    bc.height = 110
    bc.data = [[count for _, count in snap.grade_buckets]]
    bc.categoryAxis.categoryNames = [label for label, _ in snap.grade_buckets]
    bc.categoryAxis.labels.fontSize = 8
    bc.categoryAxis.labels.fillColor = INK_DIM
    bc.valueAxis.labels.fontSize = 7
    bc.valueAxis.labels.fillColor = INK_DIM
    bc.bars[0].fillColor = MINT
    bc.bars[0].strokeColor = MINT
    d.add(bc)
    return d


def _section_movers(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    flow = [_eyebrow("Markets", st), Paragraph("Top movers", st["h2"])]
    if not snap.top_gainers and not snap.top_losers:
        flow.append(
            Paragraph("No measurable price movement during this window.", st["dim"])
        )
        return flow
    flow.append(Paragraph("Gainers", st["eyebrow"]))
    flow.append(_movers_table(snap.top_gainers, is_gain=True))
    flow.append(Spacer(1, 0.25 * inch))
    flow.append(Paragraph("Losers", st["eyebrow"]))
    flow.append(_movers_table(snap.top_losers, is_gain=False))
    return flow


def _movers_table(rows, *, is_gain: bool) -> Table:
    head = ["Card", "Set", "Value", "Δ"]
    body = [
        [
            r.name,
            r.set_name or "—",
            _money(r.value_close_usd),
            f"{_money(r.delta_usd)}  ({_pct(r.delta_pct)})",
        ]
        for r in rows
    ] or [["—", "—", "—", "—"]]
    t = Table(
        [head, *body],
        colWidths=[2.6 * inch, 1.8 * inch, 1.0 * inch, 1.6 * inch],
    )
    delta_color = MINT if is_gain else ROSE
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_DIM),
                ("TEXTCOLOR", (3, 1), (3, -1), delta_color),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, LINE),
                ("LINEBELOW", (0, 1), (-1, -2), 0.2, LINE),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return t


def _section_holdings(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    flow = [_eyebrow("Positions", st), Paragraph("Holdings", st["h2"])]
    if not snap.holdings:
        flow.append(
            Paragraph(
                "Your vault is empty for this period. Scan a card to start "
                "building a statement.",
                st["dim"],
            )
        )
        return flow
    head = ["Card", "Set", "Grade", "Opening", "Closing", "Δ%"]
    body = [
        [
            h.name,
            h.set_name or "—",
            f"{h.grade:.1f}",
            _money(h.value_open_usd),
            _money(h.value_close_usd),
            _pct(h.delta_pct),
        ]
        for h in snap.holdings
    ]
    t = Table(
        [head, *body],
        colWidths=[
            2.2 * inch,
            1.6 * inch,
            0.6 * inch,
            0.9 * inch,
            0.9 * inch,
            0.8 * inch,
        ],
        repeatRows=1,
    )
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_DIM),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_ELEV]),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, LINE),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    flow.append(t)
    return flow


# ─── Page chrome (header / footer) ───────────────────────────────────────


def _make_chrome(snap: ReportSnapshot) -> Callable:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def _draw(canvas, doc) -> None:
        canvas.saveState()
        # Header rule
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, PAGE_H - 0.45 * inch, PAGE_W - MARGIN, PAGE_H - 0.45 * inch)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(INK_DIM)
        canvas.drawString(MARGIN, PAGE_H - 0.35 * inch, "LOUPE  ·  Portfolio Statement")
        canvas.drawRightString(
            PAGE_W - MARGIN,
            PAGE_H - 0.35 * inch,
            snap.period_label,
        )
        # Footer
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(INK_MUTED)
        canvas.drawString(MARGIN, 0.35 * inch, f"Generated {generated_at}")
        canvas.drawCentredString(
            PAGE_W / 2,
            0.35 * inch,
            snap.user_email,
        )
        canvas.drawRightString(
            PAGE_W - MARGIN,
            0.35 * inch,
            f"Page {doc.page}",
        )
        canvas.restoreState()

    return _draw


# ─── Public entry point ──────────────────────────────────────────────────


def _new_doc(buf: io.BytesIO, snap: ReportSnapshot) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=0.75 * inch,
        bottomMargin=0.6 * inch,
        title=f"Loupe statement — {snap.period_label}",
        author="Loupe",
    )
    frame = Frame(
        MARGIN,
        0.6 * inch,
        PAGE_W - 2 * MARGIN,
        PAGE_H - 1.35 * inch,
        id="main",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
        showBoundary=0,
    )
    doc.addPageTemplates(
        [PageTemplate(id="default", frames=[frame], onPage=_make_chrome(snap))]
    )
    return doc


def render_pdf(snap: ReportSnapshot) -> bytes:
    """Render a portfolio statement to PDF bytes, ready for upload.

    Always returns a valid PDF: if the full (charted) build hits a ReportLab
    edge case, we fall back to the text-only cover so statement generation can
    never hard-fail.
    """
    st = _styles()
    try:
        buf = io.BytesIO()
        doc = _new_doc(buf, snap)
        flow: list = []
        flow.extend(_section_cover(snap, st))
        flow.append(PageBreak())
        flow.extend(_section_performance(snap, st))
        flow.append(PageBreak())
        flow.extend(_section_allocation(snap, st))
        flow.append(PageBreak())
        flow.extend(_section_movers(snap, st))
        flow.append(PageBreak())
        flow.extend(_section_holdings(snap, st))
        doc.build(flow)
        return buf.getvalue()
    except Exception:  # pragma: no cover - defensive: never fail a statement
        # Charts are the only thing that can throw here; rebuild the summary
        # cover alone so the user always gets a downloadable statement.
        buf = io.BytesIO()
        doc = _new_doc(buf, snap)
        doc.build(list(_section_cover(snap, st)))
        return buf.getvalue()


__all__ = ["render_pdf"]
