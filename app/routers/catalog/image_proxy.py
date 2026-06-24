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
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.integrations.base import get_http_client
from app.platform.rate_limit import image_proxy_limit
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
# Redirects are followed manually (not via httpx's follow_redirects) so every
# hop can be re-checked against the host allowlist — otherwise an allowlisted
# host could 30x-redirect us to an internal address and turn the proxy into an
# SSRF relay. Card CDNs rarely redirect, so a small bound is plenty.
_MAX_REDIRECTS = 3
# Browser / expo-image cache duration. Card art is effectively immutable
# once the upstream URL exists, so we tell clients "never re-validate".
_PUBLIC_CACHE_SECONDS = 60 * 60 * 24 * 30  # 30 days

_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_cache_lock = asyncio.Lock()

# Negative cache: URLs that returned 404/410 upstream. Holding these for
# ~10 min stops the mobile client from re-requesting (and us from re-
# fetching) the same dead URL every time the tile is rendered. Pokemon
# TCG occasionally yanks `_hires.png` variants and we don't want to spam
# them with retries.
_NEG_CACHE_MAX = 512
_NEG_CACHE_TTL_SECONDS = 600
_neg_cache: OrderedDict[str, float] = OrderedDict()


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


async def _fetch_allowlisted(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET ``url``, following redirects manually so each hop is re-validated
    against the host allowlist. Without this, ``follow_redirects=True`` would let
    an allowlisted host bounce us to an internal address (SSRF). A redirect to a
    non-allowlisted host raises ``HTTPException(400)`` via :func:`_validate_url`.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        resp = await client.get(
            current, timeout=_UPSTREAM_TIMEOUT, follow_redirects=False
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return resp  # malformed redirect — let the caller handle non-200
            # Resolve relative redirects, then re-validate the destination host.
            current = _validate_url(urljoin(current, location))
            continue
        return resp
    raise HTTPException(status_code=502, detail="too_many_redirects")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.get(
    "",
    summary="Proxy a card image (public)",
    dependencies=[Depends(image_proxy_limit)],
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

    # Short-circuit recently-404'd URLs so we don't keep beating on the
    # upstream CDN — and so the mobile retry budget isn't wasted.
    import time as _time

    async with _cache_lock:
        exp = _neg_cache.get(url)
        if exp is not None and exp > _time.time():
            _neg_cache.move_to_end(url)
            raise HTTPException(status_code=404, detail="upstream_not_found")
        if exp is not None:
            # Expired — evict and fall through.
            _neg_cache.pop(url, None)

    client = await get_http_client()
    try:
        # `_fetch_allowlisted` re-validates every redirect hop; a redirect to a
        # non-allowlisted host raises HTTPException(400) here (not httpx.HTTPError),
        # which propagates straight out as a 400 — the SSRF guard.
        resp = await _fetch_allowlisted(client, url)
    except httpx.HTTPError as exc:
        logger.warning("image_proxy upstream error url=%s err=%s", url, exc)
        raise HTTPException(status_code=502, detail="upstream_unreachable") from exc

    if resp.status_code in (404, 410):
        # Upstream definitively says this image doesn't exist. Surface that
        # to the client as 404 (not 502) so it falls back to placeholder /
        # alternate URL instead of treating it as a transient failure and
        # retrying forever. Negative-cache for 10 min.
        async with _cache_lock:
            _neg_cache[url] = _time.time() + _NEG_CACHE_TTL_SECONDS
            _neg_cache.move_to_end(url)
            while len(_neg_cache) > _NEG_CACHE_MAX:
                _neg_cache.popitem(last=False)
        raise HTTPException(status_code=404, detail="upstream_not_found")

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
