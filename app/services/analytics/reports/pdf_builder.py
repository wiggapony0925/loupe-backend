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
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Polygon, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
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
MINT_DARK = colors.HexColor("#0E8A6C")  # mint that reads on light tints
MINT_TINT = colors.HexColor("#E9F9F4")  # card / chip background wash
INK_BAND = colors.HexColor("#0B0E14")  # near-black cover band
ROSE = colors.HexColor("#E5484D")
ROSE_TINT = colors.HexColor("#FDECEC")
AMBER = colors.HexColor("#F5A524")
BLUE = colors.HexColor("#2F6CFB")
WHITE = colors.white

PAGE_W, PAGE_H = LETTER
MARGIN = 0.6 * inch
CONTENT_W = PAGE_W - 2 * MARGIN


# Trading cards are 63×88mm — every embedded thumb keeps that aspect.
CARD_ASPECT = 88 / 63


def _money(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _thumb(snap: ReportSnapshot, card_id: str, width: float) -> Image | str:
    """Embedded card art for a table cell, or "" when we have no bytes."""
    data = snap.images.get(card_id)
    if not data:
        return ""
    try:
        return Image(io.BytesIO(data), width=width, height=width * CARD_ASPECT)
    except Exception:  # corrupt/unsupported bytes — render art-less
        return ""


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
        # ── Cover band (white text on the dark band) ──
        "bandBrand": ParagraphStyle(
            "bandBrand",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=15,
            textColor=MINT,
        ),
        "bandTitle": ParagraphStyle(
            "bandTitle",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=25,
            textColor=WHITE,
        ),
        "bandSub": ParagraphStyle(
            "bandSub",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#AEB6C2"),
        ),
        # ── KPI metric cards ──
        "kpiLabel": ParagraphStyle(
            "kpiLabel",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=9,
            textColor=INK_MUTED,
        ),
        "kpiValue": ParagraphStyle(
            "kpiValue",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=18,
            textColor=INK,
        ),
    }


# ─── Section renderers (each returns a list of flowables) ────────────────


def _band(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
    """Full-width dark header band that opens the cover — brand on the left,
    'Portfolio Statement' + period stacked, like a premium account statement."""
    left = [
        Paragraph("◆ LOUPE", st["bandBrand"]),
        Spacer(1, 6),
        Paragraph("Portfolio Statement", st["bandTitle"]),
        Paragraph(snap.period_label, st["bandSub"]),
    ]
    right = Paragraph(
        f'<font color="#AEB6C2" size="8">STATEMENT PERIOD</font><br/>'
        f'<font color="#FFFFFF" size="10"><b>{snap.period_start.strftime("%b %d")} – '
        f"{snap.period_end.strftime('%b %d, %Y')}</b></font>",
        st["bandSub"],
    )
    t = Table([[left, right]], colWidths=[CONTENT_W * 0.62, CONTENT_W * 0.38])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), INK_BAND),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 18),
                ("RIGHTPADDING", (0, 0), (-1, -1), 18),
                ("TOPPADDING", (0, 0), (-1, -1), 18),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
                ("ROUNDEDCORNERS", [6, 6, 6, 6]),
            ]
        )
    )
    return t


def _hero(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
    """Closing-value hero with a tinted, color-coded P/L chip beside it."""
    up = snap.delta_usd >= 0
    chip_bg = MINT_TINT if up else ROSE_TINT
    chip_fg = MINT_DARK if up else ROSE
    chip = Table(
        [
            [
                Paragraph(
                    f'<font color="{chip_fg.hexval()}" size="11"><b>'
                    f"{'▲' if up else '▼'} {_money(snap.delta_usd)}</b></font>"
                    f'  <font color="{chip_fg.hexval()}" size="10">'
                    f"({_pct(snap.delta_pct)})</font>",
                    st["body"],
                )
            ]
        ]
    )
    chip.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), chip_bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("ROUNDEDCORNERS", [10, 10, 10, 10]),
            ]
        )
    )
    left = [
        _eyebrow("Closing value", st),
        Paragraph(_money(snap.closing_value_usd), st["hero"]),
        Paragraph(f"over {snap.period_label}", st["dim"]),
    ]
    t = Table([[left, chip]], colWidths=[CONTENT_W * 0.6, CONTENT_W * 0.4])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (0, 0), "TOP"),
                ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return t


def _kpi_card(
    label: str, value: str, st: dict[str, ParagraphStyle], accent=None
) -> Table:
    card = Table(
        [
            [Paragraph(label.upper(), st["kpiLabel"])],
            [
                Paragraph(
                    f'<font color="{(accent or INK).hexval()}">{value}</font>',
                    st["kpiValue"],
                )
            ],
        ]
    )
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BG_ELEV),
                ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 11),
                ("RIGHTPADDING", (0, 0), (-1, -1), 11),
                ("TOPPADDING", (0, 0), (0, 0), 11),
                ("BOTTOMPADDING", (0, 0), (0, 0), 1),
                ("TOPPADDING", (0, 1), (0, 1), 1),
                ("BOTTOMPADDING", (0, 1), (0, 1), 12),
                ("ROUNDEDCORNERS", [6, 6, 6, 6]),
            ]
        )
    )
    return card


def _kpi_cards(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
    """A row of four at-a-glance metric cards under the hero."""
    pnl_accent = MINT_DARK if (snap.unrealized_pnl_usd or 0) >= 0 else ROSE
    cards = [
        _kpi_card("Opening value", _money(snap.opening_value_usd), st),
        _kpi_card(
            "Unrealized P/L",
            f"{_money(snap.unrealized_pnl_usd)}",
            st,
            accent=pnl_accent,
        ),
        _kpi_card("Cards in vault", f"{snap.card_count:,}", st),
        _kpi_card(
            "Avg grade",
            f"{snap.avg_grade:.1f}" if snap.avg_grade else "—",
            st,
        ),
    ]
    gap = 0.12 * inch
    cw = (CONTENT_W - 3 * gap) / 4
    row = [cards[0], "", cards[1], "", cards[2], "", cards[3]]
    t = Table([row], colWidths=[cw, gap, cw, gap, cw, gap, cw])
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return t


def _section_cover(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    return [
        _band(snap, st),
        Spacer(1, 0.30 * inch),
        _hero(snap, st),
        Spacer(1, 0.26 * inch),
        _kpi_cards(snap, st),
        Spacer(1, 0.30 * inch),
        _eyebrow("Account information", st),
        Spacer(1, 0.06 * inch),
        _account_table(snap, st),
        Spacer(1, 0.28 * inch),
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
    """Robinhood-style area chart: soft filled area under a colored value
    line, dollar-labeled gridlines, and start/end date + value callouts."""
    width = PAGE_W - 2 * MARGIN
    height = 230
    d = Drawing(width, height)

    values = [p.value_usd for p in snap.series]
    lo, hi = min(values), max(values)
    if hi <= lo:  # flat series — pad so the line sits mid-chart
        hi = lo + max(1.0, abs(lo) * 0.05)
    pad = (hi - lo) * 0.08
    lo -= pad
    hi += pad

    # Plot rect
    px, py = 58, 34
    pw, ph = width - px - 14, height - py - 26
    up = snap.delta_usd >= 0
    tone = MINT if up else ROSE
    fill = colors.Color(tone.red, tone.green, tone.blue, alpha=0.13)

    def sx(i: int) -> float:
        return px + (i / (len(values) - 1)) * pw

    def sy(v: float) -> float:
        return py + ((v - lo) / (hi - lo)) * ph

    # Gridlines + $ labels (4 bands)
    for k in range(5):
        gy = py + (k / 4) * ph
        gv = lo + (k / 4) * (hi - lo)
        d.add(Line(px, gy, px + pw, gy, strokeColor=LINE, strokeWidth=0.35))
        d.add(
            String(
                px - 6,
                gy - 2.5,
                f"${gv:,.0f}",
                fontName="Helvetica",
                fontSize=6.5,
                fillColor=INK_MUTED,
                textAnchor="end",
            )
        )

    # Area fill + value line
    pts = [(sx(i), sy(v)) for i, v in enumerate(values)]
    poly = [px, py]
    for x, y in pts:
        poly.extend([x, y])
    poly.extend([px + pw, py])
    d.add(Polygon(poly, fillColor=fill, strokeColor=None, strokeWidth=0))
    flat: list[float] = []
    for x, y in pts:
        flat.extend([x, y])
    d.add(PolyLine(flat, strokeColor=tone, strokeWidth=1.8))

    # Opening / closing callouts above the endpoints
    d.add(
        String(
            pts[0][0],
            min(height - 10, pts[0][1] + 7),
            _money(values[0]),
            fontName="Helvetica-Bold",
            fontSize=7.5,
            fillColor=INK_DIM,
        )
    )
    d.add(
        String(
            pts[-1][0],
            min(height - 10, pts[-1][1] + 7),
            _money(values[-1]),
            fontName="Helvetica-Bold",
            fontSize=7.5,
            fillColor=tone,
            textAnchor="end",
        )
    )

    # Date labels under the plot
    d.add(
        String(
            px,
            12,
            snap.series[0].date.strftime("%b %d, %Y"),
            fontName="Helvetica",
            fontSize=7,
            fillColor=INK_MUTED,
        )
    )
    d.add(
        String(
            px + pw,
            12,
            snap.series[-1].date.strftime("%b %d, %Y"),
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
        flow.append(Paragraph("No graded cards in this period yet.", st["dim"]))
    return flow


def _pie_chart(snap: ReportSnapshot) -> Drawing:
    """Donut allocation chart with a total-value center label and a legend
    carrying value + share per game."""
    width = PAGE_W - 2 * MARGIN
    d = Drawing(width, 190)
    total = sum(v for _, v in snap.tcg_breakdown) or 1.0
    pie = Pie()
    pie.x = 50
    pie.y = 15
    pie.width = 160
    pie.height = 160
    pie.innerRadiusFraction = 0.62
    pie.data = [v for _, v in snap.tcg_breakdown] or [1]
    pie.labels = None
    pie.slices.label_visible = 0
    palette = [MINT, BLUE, AMBER, ROSE, INK_DIM, INK_MUTED]
    for i, _ in enumerate(snap.tcg_breakdown):
        pie.slices[i].fillColor = palette[i % len(palette)]
        pie.slices[i].strokeColor = colors.white
        pie.slices[i].strokeWidth = 1.4
    d.add(pie)
    # Center label — total closing value inside the donut.
    cx, cy = pie.x + pie.width / 2, pie.y + pie.height / 2
    d.add(
        String(
            cx,
            cy + 3,
            _money(total if snap.tcg_breakdown else 0),
            fontName="Helvetica-Bold",
            fontSize=11,
            fillColor=INK,
            textAnchor="middle",
        )
    )
    d.add(
        String(
            cx,
            cy - 9,
            "TOTAL",
            fontName="Helvetica-Bold",
            fontSize=6,
            fillColor=INK_MUTED,
            textAnchor="middle",
        )
    )
    # Legend column on the right: swatch · game · value · share.
    cursor_y = 150
    for i, (label, value) in enumerate(snap.tcg_breakdown):
        sw = palette[i % len(palette)]
        d.add(String(260, cursor_y, "■", fontSize=13, fillColor=sw))
        d.add(
            String(
                278,
                cursor_y + 1,
                label.title(),
                fontName="Helvetica-Bold",
                fontSize=10,
                fillColor=INK,
            )
        )
        d.add(
            String(
                392,
                cursor_y + 1,
                _money(value),
                fontName="Helvetica",
                fontSize=10,
                fillColor=INK_DIM,
            )
        )
        d.add(
            String(
                width - 14,
                cursor_y + 1,
                f"{value / total * 100:.1f}%",
                fontName="Helvetica-Bold",
                fontSize=10,
                fillColor=INK,
                textAnchor="end",
            )
        )
        cursor_y -= 20
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
    flow.append(_movers_table(snap, snap.top_gainers, is_gain=True))
    flow.append(Spacer(1, 0.25 * inch))
    flow.append(Paragraph("Losers", st["eyebrow"]))
    flow.append(_movers_table(snap, snap.top_losers, is_gain=False))
    return flow


def _movers_table(snap: ReportSnapshot, rows, *, is_gain: bool) -> Table:
    head = ["", "Card", "Set", "Value", "Change"]
    body = [
        [
            _thumb(snap, r.card_id, 0.30 * inch),
            r.name,
            r.set_name or "—",
            _money(r.value_close_usd),
            f"{_money(r.delta_usd)}  ({_pct(r.delta_pct)})",
        ]
        for r in rows
    ] or [["", "—", "—", "—", "—"]]
    t = Table(
        [head, *body],
        colWidths=[0.45 * inch, 2.35 * inch, 1.6 * inch, 1.0 * inch, 1.6 * inch],
    )
    delta_color = MINT if is_gain else ROSE
    t.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                ("TEXTCOLOR", (0, 0), (-1, 0), INK_DIM),
                ("TEXTCOLOR", (4, 1), (4, -1), delta_color),
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 1), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, LINE),
                ("LINEBELOW", (0, 1), (-1, -2), 0.2, LINE),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
            ]
        )
    )
    return t


def _section_gallery(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> list:
    """Top holdings as a 4-across art gallery — the cards themselves, with
    name, grade, and closing value under each. Skipped entirely when no
    artwork could be hydrated (keeps the fallback PDF byte-identical-ish)."""
    rows = [h for h in snap.holdings[:8] if snap.images.get(h.card_id)]
    if not rows:
        return []
    flow = [
        _eyebrow("Showcase", st),
        Paragraph("Top holdings", st["h2"]),
        Paragraph("Your most valuable cards at period close.", st["dim"]),
        Spacer(1, 0.16 * inch),
    ]
    per_row = 4
    gap = 0.16 * inch
    cell_w = (CONTENT_W - (per_row - 1) * gap) / per_row
    art_w = cell_w - 0.24 * inch

    def cell(h) -> Table:
        up = h.delta_pct >= 0
        delta_hex = (MINT_DARK if up else ROSE).hexval().replace("0x", "#")
        inner = Table(
            [
                [_thumb(snap, h.card_id, art_w)],
                [
                    Paragraph(
                        f"<b>{h.name}</b><br/>"
                        f'<font size="7" color="{INK_DIM.hexval().replace("0x", "#")}">'
                        f"Grade {h.grade:.1f}</font><br/>"
                        f'<font size="8"><b>{_money(h.value_close_usd)}</b></font>  '
                        f'<font size="7" color="{delta_hex}">{_pct(h.delta_pct)}</font>',
                        ParagraphStyle(
                            "galleryCell",
                            fontName="Helvetica",
                            fontSize=8,
                            leading=10,
                            textColor=INK,
                        ),
                    )
                ],
            ],
            colWidths=[cell_w - 0.16 * inch],
        )
        inner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), BG_ELEV),
                    ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                    ("ALIGN", (0, 0), (0, 0), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (0, 0), 8),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                    ("ROUNDEDCORNERS", [6, 6, 6, 6]),
                ]
            )
        )
        return inner

    for chunk_start in range(0, len(rows), per_row):
        chunk = rows[chunk_start : chunk_start + per_row]
        line: list = []
        widths: list[float] = []
        for i, h in enumerate(chunk):
            if i:
                line.append("")
                widths.append(gap)
            line.append(cell(h))
            widths.append(cell_w)
        grid = Table([line], colWidths=widths)
        grid.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        flow.append(grid)
        flow.append(Spacer(1, gap))
    return flow


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
    head = ["Card", "Set", "Grade", "Opening", "Closing", "Chg %"]
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
    style: list = [
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), INK_DIM),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_ELEV]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, LINE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]
    # Color each Δ% cell by direction so the table scans like the app.
    for i, h in enumerate(snap.holdings, start=1):
        style.append(
            ("TEXTCOLOR", (5, i), (5, i), MINT_DARK if h.delta_pct >= 0 else ROSE)
        )
    t.setStyle(TableStyle(style))
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
        gallery = _section_gallery(snap, st)
        if gallery:
            flow.append(PageBreak())
            flow.extend(gallery)
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
