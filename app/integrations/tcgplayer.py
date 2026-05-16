"""TCGplayer Catalog + Pricing provider — market price snapshots.

API docs: https://docs.tcgplayer.com. Uses the OAuth ``client_credentials``
flow with public/private keys (mirrors of historical *client_id/secret*).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice, get_http_client
from app.utils.logger import get_logger

logger = get_logger("integrations.tcgplayer")

_TOKEN_URL = "https://api.tcgplayer.com/token"
_BASE = "https://api.tcgplayer.com"
_VERSION = "v1.39.0"


def _creds() -> tuple[str, str] | None:
    s = get_settings()
    pub = s.tcgplayer_public_key or s.tcgplayer_client_id
    priv = s.tcgplayer_private_key or s.tcgplayer_client_secret
    if pub and priv:
        return pub, priv
    return None


class TcgPlayerProvider(BaseProvider):
    id = "tcgplayer"
    name = "TCGplayer"

    def __init__(self) -> None:
        self._token: str | None = None
        self._exp: float = 0.0
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        return _creds() is not None

    async def _mint_token(self) -> str | None:
        creds = _creds()
        if not creds:
            return None
        async with self._lock:
            if self._token and time.time() < self._exp:
                return self._token
            pub, priv = creds
            try:
                client = await get_http_client()
                resp = await client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": pub,
                        "client_secret": priv,
                    },
                )
                if resp.status_code >= 400:
                    logger.warning("tcgplayer token mint failed: %s", resp.status_code)
                    return None
                body = resp.json()
                self._token = body.get("access_token")
                expires_in = int(body.get("expires_in", 1209600))  # 2 weeks
                self._exp = time.time() + max(300, expires_in - 300)
                return self._token
            except Exception as exc:
                logger.warning("tcgplayer token mint exception: %s", exc)
                return None

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        token = await self._mint_token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        # 1) Resolve product id via catalog search.
        search_url = (
            f"{_BASE}/{_VERSION}/catalog/products?productName={quote(query)}&limit=1"
        )
        try:
            r1 = await self._call_with_retry("GET", search_url, headers=headers)
            if r1 is None or r1.status_code >= 400:
                return None
            results = (r1.json() or {}).get("results") or []
            if not results:
                return None
            pid = results[0].get("productId")
            if not pid:
                return None
            # 2) Fetch pricing.
            price_url = f"{_BASE}/{_VERSION}/pricing/product/{pid}"
            r2 = await self._call_with_retry("GET", price_url, headers=headers)
            if r2 is None or r2.status_code >= 400:
                return None
            entries = (r2.json() or {}).get("results") or []
            return self._reduce(entries)
        except Exception as exc:
            logger.warning("tcgplayer get_market_price failed: %s", exc)
            return None

    @staticmethod
    def _reduce(entries: list[dict[str, Any]]) -> MarketPrice | None:
        if not entries:
            return None
        # Prefer non-foil normal print if present.
        best = next(
            (e for e in entries if e.get("subTypeName") == "Normal"),
            entries[0],
        )
        def _f(key: str) -> float | None:
            val = best.get(key)
            return float(val) if val is not None else None

        try:
            return MarketPrice(
                source="tcgplayer",
                market=_f("marketPrice"),
                low=_f("lowPrice"),
                mid=_f("midPrice"),
                high=_f("highPrice"),
                extras={"sub_type": best.get("subTypeName")},
            )
        except (TypeError, ValueError):
            return None


__all__ = ["TcgPlayerProvider"]
