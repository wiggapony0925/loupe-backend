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

import asyncio
import re
from typing import Any
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


async def photos_for_many(
    stores: list[Any],
    *,
    deadline_s: float = 3.0,
    concurrency: int = 12,
) -> None:
    """Fill ``photo_url`` on a LIST of stores, in place — fast or not at all.

    The map drawer used to show art blocks for every shop because only the
    DETAIL endpoint resolved photography; the list never did. Resolving 40
    shops serially would obviously be worse than no photos, so:

      • cached shops (the common case after the first visit) cost nothing;
      • misses resolve concurrently under a HARD deadline — whatever lands
        in time is returned, and the rest keep resolving in the background
        so they are cached and instant on the next open.

    Chains are resolved ONCE. A search around Newark returns seven GameStops,
    all pointing at the same Wikidata entity; resolving per store fetched
    that entity seven times and spent the deadline on duplicates.
    """
    pending = [s for s in stores if not s.photo_url and (s.website or s.wikidata_id)]
    if not pending:
        return

    # Group by what the photo actually comes from, so every GameStop shares
    # one lookup and the deadline is spent on DISTINCT shops. The key mirrors
    # photo_for's own priority — wikidata wins over website — because keying
    # on BOTH split the chain again: branches share a brand entity but each
    # has its own store-page URL.
    groups: dict[str, list[Any]] = {}
    for store in pending:
        key = f"wd:{store.wikidata_id}" if store.wikidata_id else f"web:{store.website}"
        groups.setdefault(key, []).append(store)

    sem = asyncio.Semaphore(concurrency)

    async def resolve(group: list[Any]) -> None:
        async with sem:
            lead = group[0]
            url = await photo_for(
                lead.id,
                osm_image=None,
                website=lead.website,
                wikidata=lead.wikidata_id,
            )
            for store in group:
                store.photo_url = url
                if store is not lead:
                    # Give each branch its own cache row so a later single
                    # store lookup is a hit too.
                    await kv_set(
                        _cache_key(store.id), url or "", ttl_seconds=CACHE_TTL_S
                    )

    tasks = [asyncio.create_task(resolve(g)) for g in groups.values()]
    _done, still_running = await asyncio.wait(tasks, timeout=deadline_s)
    if still_running:
        # Deliberately NOT cancelled: let them finish and populate the
        # week-long cache so the next request has them ready.
        logger.info("%s store photos still resolving past deadline", len(still_running))


__all__ = ["photo_for", "photos_for_many"]
