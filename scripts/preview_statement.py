"""Render a portfolio statement PDF from a synthetic snapshot — no DB needed.

Design-iteration helper for the HTML/SCSS statement. Builds a rich, realistic
``ReportSnapshot`` (a value curve, holdings, movers, allocation, grade buckets,
and a few placeholder card images so the gallery + thumbnails render), then
writes a PDF you can open to eyeball the layout while tuning the template/SCSS.

Usage (from loupe-backend/):
    python scripts/preview_statement.py [output.pdf]

On macOS WeasyPrint needs Homebrew Pango on the dylib path:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \
        python scripts/preview_statement.py
"""

from __future__ import annotations

import io
import math
import sys
from datetime import date, timedelta

from app.services.analytics.reports.aggregator import (
    HoldingRow,
    MoverRow,
    ReportSnapshot,
    SeriesPoint,
)


def _placeholder_png(seed: int) -> bytes:
    """A small colored card-shaped PNG so gallery/thumbnails have something."""
    from PIL import Image, ImageDraw

    palette = [
        (22, 192, 156),
        (47, 108, 251),
        (245, 165, 36),
        (229, 72, 77),
        (139, 92, 246),
    ]
    w, h = 252, 352  # 63x88mm card at 4x
    img = Image.new("RGB", (w, h), (247, 248, 250))
    d = ImageDraw.Draw(img)
    c = palette[seed % len(palette)]
    d.rounded_rectangle([10, 10, w - 10, h - 10], radius=18, fill=c)
    d.rounded_rectangle([28, 40, w - 28, 180], radius=10, fill=(255, 255, 255))
    d.text((30, h - 60), f"#{seed:03d}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_snapshot() -> ReportSnapshot:
    start = date(2026, 5, 1)
    end = date(2026, 5, 31)

    # A believable value curve: upward drift + a little wobble.
    series: list[SeriesPoint] = []
    base = 18_500.0
    for i in range((end - start).days + 1):
        v = base + i * 62 + math.sin(i / 3.0) * 340 + math.sin(i / 11.0) * 720
        series.append(SeriesPoint(date=start + timedelta(days=i), value_usd=v))
    opening = series[0].value_usd
    closing = series[-1].value_usd

    sets = ["Base Set", "Jungle", "Team Rocket", "Neo Genesis", "151", "Crown Zenith"]
    names = [
        "Charizard",
        "Blastoise",
        "Venusaur",
        "Pikachu",
        "Mewtwo",
        "Gengar",
        "Umbreon",
        "Rayquaza",
        "Lugia",
        "Gyarados",
        "Snorlax",
        "Dragonite",
    ]
    holdings: list[HoldingRow] = []
    images: dict[str, bytes] = {}
    for i, name in enumerate(names):
        close_v = 3200.0 / (i + 1) + 180
        open_v = close_v * (1 - (0.16 if i % 3 == 0 else -0.09))
        cid = f"card-{i}"
        holdings.append(
            HoldingRow(
                card_id=cid,
                name=name,
                set_name=sets[i % len(sets)],
                grade=10.0 - (i % 4) * 0.5,
                value_close_usd=close_v,
                value_open_usd=open_v,
                delta_pct=(close_v - open_v) / open_v * 100,
                image_url=None,
            )
        )
        if i < 8:  # gallery + a few thumbnails
            images[cid] = _placeholder_png(i)

    holdings.sort(key=lambda h: h.value_close_usd, reverse=True)

    def mover(h: HoldingRow) -> MoverRow:
        return MoverRow(
            card_id=h.card_id,
            name=h.name,
            set_name=h.set_name,
            value_close_usd=h.value_close_usd,
            delta_usd=h.value_close_usd - h.value_open_usd,
            delta_pct=h.delta_pct,
        )

    movers = sorted((mover(h) for h in holdings), key=lambda m: m.delta_pct)
    gainers = [m for m in reversed(movers) if m.delta_usd > 0][:5]
    losers = [m for m in movers if m.delta_usd < 0][:5]

    return ReportSnapshot(
        user_email="collector@example.com",
        period_label="May 2026",
        period_start=start,
        period_end=end,
        card_count=len(holdings),
        opening_value_usd=opening,
        closing_value_usd=closing,
        delta_usd=closing - opening,
        delta_pct=(closing - opening) / opening * 100,
        total_cost_usd=14_200.0,
        unrealized_pnl_usd=closing - 14_200.0,
        unrealized_pnl_pct=(closing - 14_200.0) / 14_200.0 * 100,
        avg_grade=9.2,
        series=series,
        holdings=holdings,
        top_gainers=gainers,
        top_losers=losers,
        grade_buckets=[
            ("10", 5),
            ("9-9.5", 4),
            ("8-8.5", 2),
            ("7-7.5", 1),
            ("6 & below", 0),
        ],
        tcg_breakdown=[
            ("pokemon", closing * 0.68),
            ("magic", closing * 0.22),
            ("yugioh", closing * 0.10),
        ],
        images=images,
    )


def main() -> None:
    from app.services.analytics.reports import html_builder

    out = sys.argv[1] if len(sys.argv) > 1 else "statement_preview.pdf"
    snap = build_snapshot()
    pdf = html_builder.render_statement_pdf(snap)
    with open(out, "wb") as fh:
        fh.write(pdf)
    print(f"wrote {out} ({len(pdf):,} bytes)")


if __name__ == "__main__":
    main()
