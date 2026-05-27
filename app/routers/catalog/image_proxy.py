"""Card image proxy.

A thin pass-through endpoint that fetches a card image from one of the
known upstream CDNs (Pokémon TCG, Scryfall, YGOPRODeck, TCGplayer) and
re-emits it under our own origin with aggressive cache headers.

Why this exists
---------------
* The mobile client lives on flaky cellular networks where third-party
  CDN handshakes (pokemontcg.io, ygoprodeck.com) frequently flake. The
  backend lives on a stable network and can fetch once, serve forever
  via expo-image's disk cache + our ``Cache-Control: immutable``.
* Card image URLs change rarely; once fetched they're effectively
  immutable. Long max-age (30d) is safe.
* Centralizing the proxy lets us add CDN-level caching (Cloud CDN /
  Cloudflare) in front of one URL family without touching the mobile
  client.

Security
--------
* Hard host allowlist — every other host returns 400. Prevents the
  endpoint from being abused as an open-internet SSRF relay.
* Response capped at 5 MB to prevent memory exhaustion.
* Upstream URL must be http(s) only.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.integrations.base import get_http_client
from app.platform.rate_limit import catalog_read_limit
from app.utils.logger import get_logger

logger = get_logger("routers.image_proxy")

router = APIRouter(prefix="/img", tags=["images"])


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
# Every upstream host whose images we currently surface. Adding a new
# catalog provider means adding its image host here.
_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        # Pokémon TCG (official + assets bucket)
        "images.pokemontcg.io",
        "assets.pokemontcg.io",
        # Scryfall (Magic: The Gathering)
        "cards.scryfall.io",
        "c1.scryfall.com",
        "c2.scryfall.com",
        # YGOPRODeck (Yu-Gi-Oh)
        "images.ygoprodeck.com",
        "storage.googleapis.com",  # ygoprodeck mirrors here
        # TCGplayer product imagery (used by sealed catalog)
        "tcgplayer-cdn.tcgplayer.com",
        "product-images.tcgplayer.com",
    }
)


# ---------------------------------------------------------------------------
# In-process LRU cache
# ---------------------------------------------------------------------------
# Per-worker memory cache — fast path that absorbs the dominant access
# pattern (a handful of trending cards hit by every client on startup).
# We deliberately do NOT use Redis here: storing image bytes via the
# existing `decode_responses=True` client would force base64 round-tripping
# and is not worth the complexity for a per-edge cache. The browser /
# expo-image disk cache + our 30d Cache-Control header is the durable layer.
_MAX_CACHE_ENTRIES = 256
_MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024  # 5 MB hard ceiling
_UPSTREAM_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
# Browser / expo-image cache duration. Card art is effectively immutable
# once the upstream URL exists, so we tell clients "never re-validate".
_PUBLIC_CACHE_SECONDS = 60 * 60 * 24 * 30  # 30 days

_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_cache_lock = asyncio.Lock()


async def _cache_get(url: str) -> tuple[bytes, str] | None:
    async with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        # Mark as recently used.
        _cache.move_to_end(url)
        return entry


async def _cache_put(url: str, body: bytes, content_type: str) -> None:
    async with _cache_lock:
        _cache[url] = (body, content_type)
        _cache.move_to_end(url)
        while len(_cache) > _MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_url(raw: str) -> str:
    """Return the canonical upstream URL or raise HTTPException."""
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="invalid_scheme")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="host_not_allowed")
    # Reject anything with credentials or fragments — keeps the cache
    # key clean and avoids weird upstreams.
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="invalid_url")
    return raw


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.get(
    "",
    summary="Proxy a card image (public)",
    dependencies=[Depends(catalog_read_limit)],
    response_class=Response,
)
async def proxy_image(
    u: str = Query(..., description="Upstream image URL"),
) -> Response:
    """Fetch and re-emit ``u`` as an image with long cache headers.

    Returns the raw image bytes on success. On upstream failure returns
    HTTP 502 — the mobile ``CardImage`` already retries failed loads, so
    a transient blip will recover automatically.
    """
    url = _validate_url(u)

    cached = await _cache_get(url)
    if cached is not None:
        body, content_type = cached
        return Response(
            content=body,
            media_type=content_type,
            headers={
                "Cache-Control": f"public, max-age={_PUBLIC_CACHE_SECONDS}, immutable",
                "X-Cache": "HIT",
            },
        )

    client = await get_http_client()
    try:
        resp = await client.get(url, timeout=_UPSTREAM_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning("image_proxy upstream error url=%s err=%s", url, exc)
        raise HTTPException(status_code=502, detail="upstream_unreachable") from exc

    if resp.status_code != 200:
        logger.warning("image_proxy non-200 url=%s status=%s", url, resp.status_code)
        raise HTTPException(status_code=502, detail="upstream_status")

    body = resp.content
    if len(body) > _MAX_BYTES_PER_IMAGE:
        logger.warning("image_proxy oversize url=%s bytes=%d", url, len(body))
        raise HTTPException(status_code=502, detail="upstream_too_large")

    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        # Upstream returned something weird (HTML error page, etc.)
        raise HTTPException(status_code=502, detail="upstream_not_image")

    await _cache_put(url, body, content_type)

    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Cache-Control": f"public, max-age={_PUBLIC_CACHE_SECONDS}, immutable",
            "X-Cache": "MISS",
        },
    )


__all__ = ["router"]
