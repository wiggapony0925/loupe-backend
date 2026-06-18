"""StockX provider — live listings and sold comps for graded/sealed cards.

StockX uses OAuth2 Authorization Code flow (Auth0-backed). Because this is
a *server-side* integration (no browser redirect), we use the Resource Owner
Password flow alternative: store a long-lived ``refresh_token`` as a secret
and exchange it for a fresh ``access_token`` (12h TTL) whenever the cached
one expires.

Setup checklist
---------------
1. Create an app at https://developer.stockx.com → copy Client ID + Secret.
2. On first setup, run the one-time OAuth flow (see ``scripts/stockx_auth.py``)
   to obtain the initial ``refresh_token`` → store it in ``.env``.
3. The integration then self-refreshes indefinitely.

Required env vars
-----------------
STOCKX_CLIENT_ID       — from Applications page
STOCKX_CLIENT_SECRET   — from Applications page
STOCKX_API_KEY         — x-api-key header value (shown after approval)
STOCKX_REFRESH_TOKEN   — long-lived refresh token from initial OAuth dance

All four must be set or ``is_configured()`` returns ``False`` and every method
returns ``[]`` / ``None`` gracefully.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.config import get_settings
from app.integrations.base import (
    BaseProvider,
    Listing,
    SoldComp,
    get_http_client,
    parse_grade,
)
from app.utils.logger import get_logger

logger = get_logger("integrations.stockx")

# ── OAuth endpoints ────────────────────────────────────────────────────────
_AUTH_DOMAIN = "https://accounts.stockx.com"
_TOKEN_URL = f"{_AUTH_DOMAIN}/oauth/token"
_AUDIENCE = "gateway.stockx.com"

# ── REST API ───────────────────────────────────────────────────────────────
_API_BASE = "https://api.stockx.com/v1"
_SEARCH_URL = f"{_API_BASE}/catalog/search"
_SALES_URL = f"{_API_BASE}/selling/history"

# StockX product types most relevant to TCG (trading cards + sealed product)
_CARD_PRODUCT_TYPES = {"trading-cards", "sealed-trading-cards"}

# access_token TTL from StockX docs is 43 200 s (12 h); we renew 5 min early.
_TOKEN_TTL_BUFFER_S = 300


class StockXProvider(BaseProvider):
    """StockX listings + sold-comp provider via the StockX Public API v1."""

    id = "stockx"
    name = "StockX"

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        # Serialise concurrent token refreshes so we don't fan-out N refresh
        # calls when the cache is cold.
        self._token_lock = asyncio.Lock()

    # ── configuration ──────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(
            s.stockx_client_id
            and s.stockx_client_secret
            and s.stockx_api_key
            and s.stockx_refresh_token
        )

    # ── token management ───────────────────────────────────────────────────

    async def _get_access_token(self) -> str | None:
        """Return a valid access_token, refreshing if expired."""
        async with self._token_lock:
            # Return cached token if still valid.
            if self._access_token and time.time() < self._token_expires_at:
                return self._access_token

            s = get_settings()
            try:
                client = await get_http_client()
                resp = await client.post(
                    _TOKEN_URL,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "refresh_token",
                        "client_id": s.stockx_client_id,
                        "client_secret": s.stockx_client_secret,
                        "audience": _AUDIENCE,
                        "refresh_token": s.stockx_refresh_token,
                    },
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "stockx token refresh failed: HTTP %s — %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    return None

                body = resp.json()
                self._access_token = body.get("access_token")
                # StockX returns expires_in=43200 (12 h); cache with buffer.
                expires_in = int(body.get("expires_in", 43_200))
                self._token_expires_at = time.time() + max(60, expires_in - _TOKEN_TTL_BUFFER_S)
                logger.info("stockx access_token refreshed (expires in %ds)", expires_in)
                return self._access_token

            except Exception as exc:
                logger.warning("stockx token refresh exception: %s", exc)
                return None

    def _auth_headers(self, token: str) -> dict[str, str]:
        s = get_settings()
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": s.stockx_api_key or "",
            "Content-Type": "application/json",
        }

    # ── listings ───────────────────────────────────────────────────────────

    async def search_listings(self, query: str, *, limit: int = 20) -> list[Listing]:
        """Search StockX for active buy-now listings matching *query*."""
        if not self.is_configured() or not query:
            return []

        token = await self._get_access_token()
        if not token:
            return []

        try:
            client = await get_http_client()
            resp = await client.get(
                _SEARCH_URL,
                headers=self._auth_headers(token),
                params={
                    "query": query[:150],
                    "pageSize": min(limit, 40),
                    "page": 1,
                    # Only return immediately purchasable listings.
                    "listingType": "fixed",
                },
            )

            if resp.status_code == 401:
                # Access token was rejected — clear cache so next call retries.
                self._access_token = None
                logger.warning("stockx got 401; token invalidated for next call")
                return []

            if resp.status_code >= 400:
                logger.warning("stockx search_listings HTTP %s", resp.status_code)
                return []

            data = resp.json()
            products = data.get("Products") or data.get("results") or []
            out: list[Listing] = []
            for product in products:
                listing = _parse_product_listing(product)
                if listing:
                    out.append(listing)

            out.sort(key=lambda x: x.price)
            return out[:limit]

        except Exception as exc:
            logger.warning("stockx search_listings exception: %s", exc)
            return []

    # ── sold comps ─────────────────────────────────────────────────────────

    async def search_sold_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        """Return recent StockX sales matching *query*."""
        if not self.is_configured() or not query:
            return []

        token = await self._get_access_token()
        if not token:
            return []

        try:
            client = await get_http_client()
            resp = await client.get(
                _SEARCH_URL,
                headers=self._auth_headers(token),
                params={
                    "query": query[:150],
                    "pageSize": min(limit, 40),
                    "page": 1,
                    "listingType": "auction",
                    "state": "sold",
                },
            )

            if resp.status_code == 401:
                self._access_token = None
                logger.warning("stockx got 401 on comps; token invalidated")
                return []

            if resp.status_code >= 400:
                logger.warning("stockx search_sold_comps HTTP %s", resp.status_code)
                return []

            data = resp.json()
            products = data.get("Products") or data.get("results") or []
            out: list[SoldComp] = []
            for product in products:
                comp = _parse_product_comp(product)
                if comp:
                    out.append(comp)

            out.sort(key=lambda x: x.sold_at, reverse=True)
            return out[:limit]

        except Exception as exc:
            logger.warning("stockx search_sold_comps exception: %s", exc)
            return []


# ── parsers ────────────────────────────────────────────────────────────────

def _parse_product_listing(product: dict[str, Any]) -> Listing | None:
    """Map a StockX product search result to a ``Listing``."""
    try:
        # Price may live in different shapes depending on API version.
        price_obj = (
            product.get("market", {}).get("lowestAsk")
            or product.get("lowestAsk")
            or product.get("retailPrice")
            or {}
        )
        if isinstance(price_obj, (int, float)):
            amount = float(price_obj)
            currency = "USD"
        else:
            amount = float(price_obj.get("amount") or price_obj.get("value") or 0)
            currency = str(price_obj.get("currency") or "USD")

        if amount <= 0:
            return None

        title = product.get("title") or product.get("name") or ""
        url = _product_url(product)
        image_url = _product_image(product)

        return Listing(
            source="stockx",
            title=title,
            price=round(amount, 2),
            currency=currency,
            url=url or "",
            condition="Near Mint",  # StockX only lists NM/sealed by definition
            image_url=image_url,
            is_auction=False,  # StockX uses fixed buy-now pricing
            time_left_seconds=None,
        )
    except Exception:
        return None


def _parse_product_comp(product: dict[str, Any]) -> SoldComp | None:
    """Map a StockX sold-sale result to a ``SoldComp``."""
    try:
        price_obj = (
            product.get("lastSale")
            or product.get("market", {}).get("lastSale")
            or product.get("salesThisPeriod")
            or {}
        )
        if isinstance(price_obj, (int, float)):
            amount = float(price_obj)
            currency = "USD"
        else:
            amount = float(price_obj.get("amount") or price_obj.get("value") or 0)
            currency = str(price_obj.get("currency") or "USD")

        if amount <= 0:
            return None

        title = product.get("title") or product.get("name") or ""
        sold_at = str(product.get("lastSaleDate") or product.get("updatedAt") or "")
        house, grade = parse_grade(title)

        return SoldComp(
            source="stockx",
            title=title,
            price=round(amount, 2),
            sold_at=sold_at,
            currency=currency,
            condition="Near Mint",
            grade=grade,
            house=house,
            url=_product_url(product),
            image_url=_product_image(product),
        )
    except Exception:
        return None


def _product_url(product: dict[str, Any]) -> str | None:
    """Extract a canonical StockX product URL."""
    slug = product.get("urlKey") or product.get("slug") or product.get("url_key")
    if slug:
        return f"https://stockx.com/product/{slug}"
    direct = product.get("url") or product.get("permalink")
    return direct or None


def _product_image(product: dict[str, Any]) -> str | None:
    """Extract the primary product image URL."""
    media = product.get("media") or {}
    return (
        media.get("imageUrl")
        or media.get("thumbUrl")
        or product.get("image")
        or product.get("imageUrl")
        or None
    )


__all__ = ["StockXProvider"]
