"""Public entry point for rendering a portfolio-statement PDF.

The statement is rendered from **HTML + SCSS** by WeasyPrint (see
:mod:`app.services.analytics.reports.html_builder`) — proper vector charts,
real data, easy to evolve. This module is the thin orchestrator that everything
else imports (``render_pdf``), plus a **slim ReportLab fallback** so a statement
can *never* hard-fail: if WeasyPrint's native libraries are missing or the HTML
build raises, we still emit a valid cover-summary PDF.

Keeping the fallback on pure-Python ReportLab (no system libs) means local dev
and CI produce a statement even without Pango installed.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.analytics.reports.aggregator import ReportSnapshot
from app.utils.logger import get_logger

_log = get_logger("services.reports.pdf")

# ─── Brand palette (fallback cover only) ─────────────────────────────────
INK = colors.HexColor("#0E1117")
INK_DIM = colors.HexColor("#5B6470")
INK_MUTED = colors.HexColor("#9BA3AD")
LINE = colors.HexColor("#E5E8EC")
BG_ELEV = colors.HexColor("#F7F8FA")
MINT = colors.HexColor("#16C09C")
MINT_DARK = colors.HexColor("#0E8A6C")
MINT_TINT = colors.HexColor("#E9F9F4")
INK_BAND = colors.HexColor("#0B0E14")
ROSE = colors.HexColor("#E5484D")
ROSE_TINT = colors.HexColor("#FDECEC")
WHITE = colors.white

PAGE_W, PAGE_H = LETTER
MARGIN = 0.6 * inch
CONTENT_W = PAGE_W - 2 * MARGIN


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


# ─── Public entry point ──────────────────────────────────────────────────


def render_pdf(snap: ReportSnapshot) -> bytes:
    """Render a portfolio statement to PDF bytes, ready for upload.

    Primary path is the HTML/SCSS/WeasyPrint renderer. If that path is
    unavailable (missing native libs) or raises, we fall back to a slim
    ReportLab cover so statement generation never hard-fails.
    """
    try:
        from app.services.analytics.reports import html_builder

        return html_builder.render_statement_pdf(snap)
    except Exception:
        _log.exception("HTML statement render failed; falling back to ReportLab cover")
        return _fallback_pdf(snap)


def _fallback_pdf(snap: ReportSnapshot) -> bytes:
    """Cover-summary-only PDF: brand band, hero, KPIs, account info + summary."""
    st = _styles()
    buf = io.BytesIO()
    doc = _new_doc(buf, snap)
    doc.build(list(_section_cover(snap, st)))
    return buf.getvalue()


# ─── ReportLab cover section (fallback) ──────────────────────────────────


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
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


def _eyebrow(text: str, st: dict[str, ParagraphStyle]) -> Paragraph:
    return Paragraph(text.upper(), st["eyebrow"])


def _band(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
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


def _account_table(snap: ReportSnapshot, st: dict[str, ParagraphStyle]) -> Table:
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


def _make_chrome(snap: ReportSnapshot):
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def _draw(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN, PAGE_H - 0.45 * inch, PAGE_W - MARGIN, PAGE_H - 0.45 * inch)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(INK_DIM)
        canvas.drawString(MARGIN, PAGE_H - 0.35 * inch, "LOUPE  ·  Portfolio Statement")
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 0.35 * inch, snap.period_label)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(INK_MUTED)
        canvas.drawString(MARGIN, 0.35 * inch, f"Generated {generated_at}")
        canvas.drawCentredString(PAGE_W / 2, 0.35 * inch, snap.user_email)
        canvas.drawRightString(PAGE_W - MARGIN, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    return _draw


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


__all__ = ["render_pdf"]
