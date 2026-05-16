"""eBay Browse + Marketplace Insights provider.

Browse API (free) powers live listings; Marketplace Insights (limited
access) powers sold comps. When credentials are missing, both methods
return ``[]`` so the rest of the stack falls back to synthesis.
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import (
    BaseProvider,
    Listing,
    SoldComp,
    get_http_client,
    parse_grade,
)
from app.utils.logger import get_logger

logger = get_logger("integrations.ebay")

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_INSIGHTS_URL = (
    "https://api.ebay.com/buy/marketplace_insights/v1_beta/item_sales/search"
)
# eBay category — "Collectible Card Games" (TCG umbrella).
_CATEGORY_ID = "183454"


class EbayProvider(BaseProvider):
    id = "ebay"
    name = "eBay"

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        s = get_settings()
        return bool(s.ebay_oauth_token or (s.ebay_app_id and s.ebay_cert_id))

    # ---- token --------------------------------------------------------

    async def _mint_token(self) -> str | None:
        s = get_settings()
        if s.ebay_oauth_token:
            return s.ebay_oauth_token
        if not (s.ebay_app_id and s.ebay_cert_id):
            return None
        async with self._token_lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            basic = base64.b64encode(
                f"{s.ebay_app_id}:{s.ebay_cert_id}".encode()
            ).decode()
            try:
                client = await get_http_client()
                resp = await client.post(
                    _TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {basic}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={
                        "grant_type": "client_credentials",
                        "scope": "https://api.ebay.com/oauth/api_scope",
                    },
                )
                if resp.status_code >= 400:
                    logger.warning("ebay token mint failed: %s", resp.status_code)
                    return None
                body = resp.json()
                self._token = body.get("access_token")
                expires_in = int(body.get("expires_in", 7200))
                self._token_expires_at = time.time() + max(60, expires_in - 60)
                return self._token
            except Exception as exc:
                logger.warning("ebay token mint exception: %s", exc)
                return None

    async def _headers(self) -> dict[str, str] | None:
        token = await self._mint_token()
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    # ---- listings -----------------------------------------------------

    async def search_listings(self, query: str, *, limit: int = 20) -> list[Listing]:
        if not self.is_configured() or not query:
            return []
        headers = await self._headers()
        if not headers:
            return []
        url = (
            f"{_BROWSE_URL}?q={quote(query)}&category_ids={_CATEGORY_ID}&limit={limit}"
        )
        try:
            resp = await self._call_with_retry("GET", url, headers=headers)
            if resp is None or resp.status_code >= 400:
                return []
            data = resp.json() or {}
            out: list[Listing] = []
            for item in (data.get("itemSummaries") or [])[:limit]:
                price_obj = item.get("price") or {}
                try:
                    amt = float(price_obj.get("value") or 0)
                except (TypeError, ValueError):
                    continue
                if amt <= 0:
                    continue
                buying = item.get("buyingOptions") or []
                is_auction = "AUCTION" in buying
                images = item.get("image") or {}
                out.append(
                    Listing(
                        source="ebay",
                        title=item.get("title") or "",
                        price=round(amt, 2),
                        currency=price_obj.get("currency") or "USD",
                        url=item.get("itemWebUrl") or "",
                        condition=item.get("condition"),
                        image_url=images.get("imageUrl"),
                        is_auction=is_auction,
                    )
                )
            return out
        except Exception as exc:
            logger.warning("ebay search_listings failed: %s", exc)
            return []

    # ---- sold comps ---------------------------------------------------

    async def search_sold_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        if not self.is_configured() or not query:
            return []
        headers = await self._headers()
        if not headers:
            return []
        now = datetime.now(UTC)
        since = now - timedelta(days=days)
        filt = (
            f"lastSoldDate:[{since.strftime('%Y-%m-%dT%H:%M:%SZ')}.."
            f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}]"
        )
        url = f"{_INSIGHTS_URL}?q={quote(query)}&filter={quote(filt)}&limit={limit}"
        try:
            resp = await self._call_with_retry("GET", url, headers=headers)
            if resp is None or resp.status_code >= 400:
                return []
            data = resp.json() or {}
            return [
                comp
                for comp in (
                    self._parse_sale(it) for it in (data.get("itemSales") or [])
                )
                if comp is not None
            ][:limit]
        except Exception as exc:
            logger.warning("ebay search_sold_comps failed: %s", exc)
            return []

    @staticmethod
    def _parse_sale(item: dict[str, Any]) -> SoldComp | None:
        try:
            price_obj = item.get("lastSoldPrice") or item.get("price") or {}
            amt = float(price_obj.get("value") or 0)
            if amt <= 0:
                return None
            title = item.get("title") or ""
            house, grade = parse_grade(title)
            sold_at = item.get("lastSoldDate") or item.get("itemEndDate") or ""
            if sold_at and not sold_at.endswith("Z"):
                if sold_at.endswith("+00:00"):
                    sold_at = sold_at[:-6] + "Z"
                else:
                    sold_at = sold_at + "Z"
            images = item.get("image") or {}
            return SoldComp(
                source="ebay",
                title=title,
                price=round(amt, 2),
                sold_at=sold_at,
                currency=price_obj.get("currency") or "USD",
                condition=item.get("condition"),
                grade=grade,
                house=house,
                url=item.get("itemWebUrl"),
                image_url=images.get("imageUrl"),
            )
        except Exception:
            return None


__all__ = ["EbayProvider"]
