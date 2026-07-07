"""Best-effort card-art hydration for statement PDFs.

Fetches the card images a statement wants to embed (top movers + the
top-holdings gallery) into ``snapshot.images`` (card_id → raw bytes).
Runs async *before* the sync ReportLab render so the event loop never
blocks on the network, and never raises: a missing image simply renders
as an art-less row, exactly like before.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.services.analytics.reports.aggregator import ReportSnapshot

_log = logging.getLogger(__name__)

# Keep statements fast + bounded: at most this many unique images per PDF.
MAX_IMAGES = 20
# Reject anything that isn't plausibly a small card scan.
MAX_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT_S = 6.0
CONCURRENCY = 6

# How many top holdings get artwork (the "top holdings" gallery page).
GALLERY_HOLDINGS = 8


def _wanted_urls(snap: ReportSnapshot) -> dict[str, str]:
    """card_id → image_url for every row the renderer will draw art for."""
    wanted: dict[str, str] = {}
    rows = [
        *snap.top_gainers,
        *snap.top_losers,
        *snap.holdings[:GALLERY_HOLDINGS],
    ]
    for row in rows:
        url = getattr(row, "image_url", None)
        if url and row.card_id not in wanted:
            wanted[row.card_id] = url
        if len(wanted) >= MAX_IMAGES:
            break
    return wanted


async def _fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    card_id: str,
    url: str,
) -> tuple[str, bytes | None]:
    async with sem:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                return card_id, None
            ctype = res.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                return card_id, None
            if len(res.content) > MAX_BYTES:
                return card_id, None
            return card_id, res.content
        except Exception:
            return card_id, None


async def hydrate_report_images(snap: ReportSnapshot) -> None:
    """Populate ``snap.images`` in place. Never raises."""
    try:
        wanted = _wanted_urls(snap)
        if not wanted:
            return
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "loupe-reports/1.0"},
        ) as client:
            results = await asyncio.gather(
                *(_fetch_one(client, sem, cid, url) for cid, url in wanted.items())
            )
        for card_id, data in results:
            if data:
                snap.images[card_id] = data
        _log.info(
            "statement images hydrated: %d/%d fetched",
            len(snap.images),
            len(wanted),
        )
    except Exception:  # pragma: no cover — art is always optional
        _log.warning("statement image hydration failed; rendering without art")


__all__ = ["GALLERY_HOLDINGS", "hydrate_report_images"]
