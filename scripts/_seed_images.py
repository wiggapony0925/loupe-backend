"""Real photographs for seeded content, from Wikimedia Commons.

**Why Commons and not a Google Images scrape.** Seeded posts want photos
that look like something a collector actually took — a binder page, a shop
counter, cards fanned on a table — not catalog scans. Commons gives exactly
that, and three things a scrape can't:

* **A licence you can point at.** Every file states its terms, so nothing
  copyrighted-and-unlicensed ends up re-hosted inside the product. Scraping
  Google returns whatever the crawler found, licence unknown.
* **A real API.** No HTML selectors to rot, no rate-limit games, no
  user-agent theatre. Commons asks only that you identify yourself.
* **Attribution data in the response**, so CC BY files can be credited
  properly rather than silently stripped.

Files that require attribution carry it back with them (see
:class:`SeedImage.credit`); the caller decides how to display it. Public
domain and CC0 files come back with ``credit=None``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

#: Commons asks API clients to identify themselves with a contact URL.
USER_AGENT = "LoupeSeed/1.0 (https://loupe.app; community feed seed data)"

#: Searches that return photos of the thing this app is about. Each is a
#: separate query so one dud term can't starve the pool.
SEARCHES: tuple[str, ...] = (
    "pokemon trading card game",
    "trading card collection",
    "card binder collection",
    "yu-gi-oh card",
    "magic the gathering cards",
    "card shop store",
    "trading card album",
    "collectible card game play",
)

#: Licences we'll re-host. Everything here is free to use commercially;
#: the BY/BY-SA ones require the credit we carry back.
_ALLOWED = ("public domain", "cc0", "cc by", "cc-by", "cc sa", "cc-sa")

#: Width to request. Big enough to look like a photo on a phone, small
#: enough that a hundred of them download quickly.
THUMB_WIDTH = 1200

#: A full-text search for "card shop" also returns SD cards, card readers
#: and a chip called HuC1A. The filename is the cheapest reliable signal of
#: what a Commons photo is actually of, so a result has to name the subject
#: to make the cut — a seeded feed full of circuit boards is worse than a
#: smaller one full of cards.
#: STRONG signals only. A bare "card" match let through a Victorian
#: advertising "trade card" and a 1962 trade-union membership card — both
#: real photos, neither remotely a trading card. The subject has to be
#: named outright.
_ON_TOPIC = (
    "trading card",
    "tcg",
    "pokemon",
    "pokémon",
    "yugioh",
    "yu-gi-oh",
    "yu gi oh",
    "magic the gathering",
    "mtg",
    "booster",
    "card game",
    "card shop",
    "card binder",
)

#: …and these are the "card" hits that aren't trading cards at all.
_OFF_TOPIC = (
    "sd card",
    "memory card",
    "card reader",
    "credit card",
    "graphics card",
    "sim card",
    "circuit",
    "chip",
    "motherboard",
    "postcard",
    "business card",
    "id card",
    "punch card",
    "trade card",  # Victorian advertising cards
    "trade union",
    "membership card",
    "playing card",
    "tarot",
    "report card",
    "cardboard",
)


@dataclass(frozen=True)
class SeedImage:
    url: str
    title: str
    #: "© Artist (CC BY-SA 4.0)" when the licence requires it, else None.
    credit: str | None


def _needs_credit(licence: str) -> bool:
    low = licence.lower()
    return "cc" in low and "zero" not in low and "cc0" not in low


async def _search(client: httpx.AsyncClient, term: str, limit: int) -> list[SeedImage]:
    try:
        resp = await client.get(
            COMMONS_API,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {term}",
                "gsrlimit": limit,
                "gsrnamespace": 6,  # File:
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "iiurlwidth": THUMB_WIDTH,
                "format": "json",
            },
            timeout=25.0,
        )
        resp.raise_for_status()
        pages = (resp.json().get("query") or {}).get("pages") or {}
    except Exception:
        return []

    out: list[SeedImage] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if not url:
            continue
        meta = info.get("extmetadata") or {}
        licence = (meta.get("LicenseShortName") or {}).get("value", "")
        if not any(tag in licence.lower() for tag in _ALLOWED):
            continue
        # Very small files are usually logos or icons, not photographs.
        if (info.get("thumbwidth") or 0) < 500:
            continue
        title_low = page.get("title", "").lower()
        if not any(word in title_low for word in _ON_TOPIC):
            continue
        if any(word in title_low for word in _OFF_TOPIC):
            continue
        artist = (meta.get("Artist") or {}).get("value", "")
        # The Artist field is HTML; a crude strip is enough for a credit line.
        artist = _strip_html(artist)[:60] or "Wikimedia Commons"
        out.append(
            SeedImage(
                url=url,
                title=page.get("title", ""),
                credit=(f"📷 {artist} ({licence})" if _needs_credit(licence) else None),
            )
        )
    return out


def _strip_html(value: str) -> str:
    depth = 0
    chars: list[str] = []
    for ch in value:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            chars.append(ch)
    return " ".join("".join(chars).split())


async def gather(limit_per_search: int = 30) -> list[SeedImage]:
    """Every usable photo across all searches, de-duplicated by URL."""
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        batches = await asyncio.gather(
            *(_search(client, term, limit_per_search) for term in SEARCHES)
        )
    seen: dict[str, SeedImage] = {}
    for batch in batches:
        for image in batch:
            seen.setdefault(image.url, image)
    return list(seen.values())


__all__ = ["THUMB_WIDTH", "SeedImage", "gather"]
