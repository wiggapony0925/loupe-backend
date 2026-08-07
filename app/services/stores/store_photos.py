"""Store photography — best-effort, free, and cached.

OpenStreetMap has no photos, so a card shop's picture comes from what the
shop itself publishes: an ``image`` tag on the OSM object when a mapper
added one, otherwise the ``og:image`` its own website advertises (the same
signal every link-preview does). Nothing is scraped beyond the document
head, results are cached for a week, and a miss simply means the client
renders its themed art block.

No API keys, no per-request billing — the whole locator stays $0.
"""

from __future__ import annotations

import re

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.utils.logger import get_logger

logger = get_logger("services.stores.photos")

CACHE_TTL_S = 7 * 24 * 3600
FETCH_TIMEOUT_S = 4.0
#: Only the head matters; stop well before a heavy page body.
MAX_HTML_BYTES = 60_000
USER_AGENT = "Loupe/1.0 (card-shop locator; https://loupe.app)"

_OG_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]*content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_OG_RE_REVERSED = re.compile(
    r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"']og:image[\"']",
    re.IGNORECASE,
)


def _cache_key(store_id: str) -> str:
    return f"stores:photo:v1:{store_id}"


def _absolutize(url: str, site: str) -> str | None:
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        base = site.rstrip("/")
        # Strip any path from the site root.
        parts = base.split("/")
        if len(parts) >= 3:
            base = "/".join(parts[:3])
        return f"{base}{url}"
    return None


async def _og_image(site: str) -> str | None:
    """The og:image a site advertises, or None."""
    url = site if site.startswith("http") else f"https://{site}"
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text[:MAX_HTML_BYTES]
    except Exception as exc:  # site down / blocks us / not HTML → no photo
        logger.info("no og:image for %s (%s)", site, exc)
        return None

    match = _OG_RE.search(html) or _OG_RE_REVERSED.search(html)
    if not match:
        return None
    return _absolutize(match.group(1).strip(), url)


async def photo_for(
    store_id: str, *, osm_image: str | None, website: str | None
) -> str | None:
    """Best photo URL for a store — cached a week, ``None`` when there is none.

    Cached NEGATIVES too (empty string): a shop with no website shouldn't
    cost a fetch attempt on every open.
    """
    key = _cache_key(store_id)
    cached = await kv_get(key)
    if cached is not None:
        return cached or None

    found = osm_image
    if not found and website:
        found = await _og_image(website)

    await kv_set(key, found or "", ttl_seconds=CACHE_TTL_S)
    return found


__all__ = ["photo_for"]
