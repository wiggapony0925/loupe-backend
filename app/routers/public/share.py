"""Server-rendered Open Graph pages for card-detail share links.

A single-page app can't give link-preview crawlers (iMessage, Slack, Discord,
Twitter/X, Facebook, WhatsApp…) per-card metadata — they don't run JS, so they
only ever see the static shell's default banner. nginx routes crawler requests
for ``/cards/:id`` here; we return a tiny HTML page whose ``<head>`` carries the
card's *own* image + name + price as the OG/Twitter tags, and which redirects a
human straight back to the SPA.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.catalog import card_search_service

router = APIRouter(prefix="/share", tags=["share"])

# Card lookup may touch an upstream catalog on a cold cache. Crawlers tolerate a
# few seconds and the rendered page is cached (10 min), so we allow a generous
# budget — better a correct per-card preview than the generic banner fallback.
_FETCH_TIMEOUT_S = 6.0

_TCG_LABEL = {
    "pokemon": "Pokémon",
    "magic": "Magic: The Gathering",
    "yugioh": "Yu-Gi-Oh!",
}


def _best_image(card: dict[str, Any]) -> str | None:
    images = card.get("images") or {}
    for band in ("large", "normal", "small"):
        v = images.get(band)
        if isinstance(v, dict) and v.get("url"):
            return str(v["url"])
    return card.get("image_url")


def _price_label(card: dict[str, Any]) -> str | None:
    pricing = card.get("pricing_summary") or {}
    for band in ("market", "high", "mid", "low"):
        v = pricing.get(band)
        if isinstance(v, dict) and v.get("amount") is not None:
            try:
                return f"${float(v['amount']):,.2f}"
            except (TypeError, ValueError):
                continue
    return None


def _render(*, title: str, desc: str, image: str | None, url: str) -> str:
    e = html.escape
    if image:
        image_tags = (
            f'<meta property="og:image" content="{e(image)}"/>'
            f'<meta property="og:image:alt" content="{e(title)}"/>'
            f'<meta name="twitter:image" content="{e(image)}"/>'
            '<meta name="twitter:card" content="summary_large_image"/>'
        )
    else:
        image_tags = '<meta name="twitter:card" content="summary"/>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"/>'
        f"<title>{e(title)}</title>"
        f'<meta name="description" content="{e(desc)}"/>'
        '<meta property="og:type" content="product"/>'
        '<meta property="og:site_name" content="Loupe"/>'
        f'<meta property="og:title" content="{e(title)}"/>'
        f'<meta property="og:description" content="{e(desc)}"/>'
        f'<meta property="og:url" content="{e(url)}"/>'
        f'<meta name="twitter:title" content="{e(title)}"/>'
        f'<meta name="twitter:description" content="{e(desc)}"/>'
        f"{image_tags}"
        f'<meta http-equiv="refresh" content="0; url={e(url)}"/>'
        f'<link rel="canonical" href="{e(url)}"/>'
        "</head><body>"
        f'<p>Redirecting to <a href="{e(url)}">{e(title)}</a>…</p>'
        "</body></html>"
    )


@router.get(
    "/card/{card_id:path}", response_class=HTMLResponse, include_in_schema=False
)
async def card_share(card_id: str) -> HTMLResponse:
    """OG page for a single card. Never errors — falls back to the brand banner."""
    settings = get_settings()
    web = settings.app_public_url.rstrip("/")
    url = f"{web}/cards/{card_id}"

    # Defaults (the generic banner) — used if the card can't be resolved.
    title = "Loupe — Scan, value & track your trading cards"
    desc = "Live, grade-aware market values for Pokémon, Magic & Yu-Gi-Oh! cards."
    image: str | None = f"{web}/og-image.png"

    try:
        card = await asyncio.wait_for(
            card_search_service.get_card(card_id), _FETCH_TIMEOUT_S
        )
    except Exception:
        card = None

    if card:
        name = card.get("name") or "Card"
        set_name = card.get("set_name")
        title = name + (f" · {set_name}" if set_name else "") + " | Loupe"
        tcg = _TCG_LABEL.get(card.get("tcg", ""), str(card.get("tcg") or "").title())
        price = _price_label(card)
        bits = [b for b in (tcg, card.get("rarity"), price) if b]
        if bits:
            desc = " · ".join(bits) + " — live, grade-aware value on Loupe."
        card_image = _best_image(card)
        if card_image:
            image = card_image

    return HTMLResponse(
        _render(title=title, desc=desc, image=image, url=url),
        headers={"Cache-Control": "public, max-age=600, stale-while-revalidate=86400"},
    )


__all__ = ["router"]
