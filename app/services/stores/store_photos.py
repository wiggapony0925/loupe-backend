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
from urllib.parse import quote

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.utils.logger import get_logger

logger = get_logger("services.stores.photos")

CACHE_TTL_S = 7 * 24 * 3600
FETCH_TIMEOUT_S = 4.0
#: Only the head matters; stop well before a heavy page body.
MAX_HTML_BYTES = 60_000
USER_AGENT = "Loupe/1.0 (card-shop locator; https://loupe.app)"

#: og:image first, then twitter:image, then the apple touch icon — most
#: small shops publish at least one of the three.
_META_KEYS = ("og:image", "twitter:image", "twitter:image:src")
_META_FORWARD = [
    re.compile(
        rf"<meta[^>]+(?:property|name)=[\"']{re.escape(k)}[\"'][^>]*content=[\"']([^\"']+)[\"']",
        re.IGNORECASE,
    )
    for k in _META_KEYS
]
_META_REVERSED = [
    re.compile(
        rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"']{re.escape(k)}[\"']",
        re.IGNORECASE,
    )
    for k in _META_KEYS
]
_APPLE_ICON_RE = re.compile(
    r"<link[^>]+rel=[\"']apple-touch-icon[^\"']*[\"'][^>]*href=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

#: Kept so the unit tests can assert both attribute orders still parse.
_OG_RE = _META_FORWARD[0]
_OG_RE_REVERSED = _META_REVERSED[0]


def _cache_key(store_id: str) -> str:
    return f"stores:photo:v3:{store_id}"


def _absolutize(url: str, site: str) -> str | None:
    """Absolute HTTPS URL, or None when the value isn't a fetchable image.

    HTTPS is forced: sites routinely advertise ``http://`` og:image URLs,
    and iOS App Transport Security silently refuses to load those — which
    looked exactly like "the images don't work".
    """
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return f"https://{url[len('http://') :]}"
    if url.startswith("https://"):
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

    for pattern in (*_META_FORWARD, *_META_REVERSED, _APPLE_ICON_RE):
        match = pattern.search(html)
        if match:
            resolved = _absolutize(match.group(1).strip(), url)
            if resolved:
                return resolved
    return None


async def _wikimedia_image(qid: str) -> str | None:
    """A real photo (or logo) for a Wikidata entity — free, no key.

    OSM tags chain stores with ``brand:wikidata`` (and some independents
    with ``wikidata``). P18 is an actual photograph of the subject, P154
    the logo. Photo wins — a storefront beats a wordmark — and either
    beats the drawn placeholder.
    """
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(
                f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
            )
            resp.raise_for_status()
            claims = resp.json()["entities"][qid]["claims"]
    except Exception as exc:
        logger.info("wikidata miss for %s (%s)", qid, exc)
        return None

    for prop in ("P18", "P154"):  # photograph first, then logo
        try:
            name = claims[prop][0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(name, str) and name:
            return (
                "https://commons.wikimedia.org/wiki/Special:FilePath/"
                f"{quote(name.replace(' ', '_'))}?width=800"
            )
    return None


async def photo_for(
    store_id: str,
    *,
    osm_image: str | None,
    website: str | None,
    wikidata: str | None = None,
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
    if not found and wikidata:
        # Chains carry brand:wikidata → Commons has real photography.
        found = await _wikimedia_image(wikidata)
    if not found and website:
        found = await _og_image(website)

    await kv_set(key, found or "", ttl_seconds=CACHE_TTL_S)
    return found


__all__ = ["photo_for"]
