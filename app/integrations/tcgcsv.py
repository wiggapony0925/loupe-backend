"""TCGCSV provider — free TCGplayer market prices via tcgcsv.com daily dumps.

TCGCSV (https://tcgcsv.com) publishes the entire TCGplayer catalog + prices
as CSV/JSON every day, no key required. We lazily download a per-category
products+prices snapshot, cache it in memory, and serve ``get_market_price``
by name match.

Categories used:
    3   = Pokémon
    1   = Magic
    2   = YuGiOh

Disable with ``TCGCSV_ENABLED=false`` (default: enabled — it's free).
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from typing import Any

from app.config import get_settings
from app.integrations.base import BaseProvider, MarketPrice, get_http_client
from app.utils.logger import get_logger

logger = get_logger("integrations.tcgcsv")

_BASE = "https://tcgcsv.com/tcgplayer"
_CATEGORIES: dict[str, int] = {"pokemon": 3, "magic": 1, "yugioh": 2}

# Cache TTL — TCGCSV refreshes once a day.
_CACHE_TTL_SECONDS = 6 * 60 * 60
# Hard cap so a misconfigured key doesn't blow memory.
_MAX_ROWS_PER_CATEGORY = 200_000


class _Cache:
    def __init__(self) -> None:
        self._loaded_at: float = 0.0
        # name (lowercased) -> {"market": float|None, "low": float|None,
        #                       "mid": float|None, "high": float|None, "url": str}
        self._by_name: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def fresh(self) -> bool:
        return self._by_name and (time.time() - self._loaded_at) < _CACHE_TTL_SECONDS

    def lookup(self, query: str) -> dict[str, Any] | None:
        if not query:
            return None
        q = query.lower().strip()
        # Exact match first
        hit = self._by_name.get(q)
        if hit:
            return hit
        # Cheap contains-fallback — return the first row containing every
        # token of the query. Keeps lookup O(n) per call; n ~ 50–150k rows.
        tokens = [t for t in q.split() if t]
        if not tokens:
            return None
        for name, row in self._by_name.items():
            if all(t in name for t in tokens):
                return row
        return None


_cache = _Cache()


class TcgCsvProvider(BaseProvider):
    id = "tcgcsv"
    name = "TCGCSV (TCGplayer mirror)"

    def is_configured(self) -> bool:
        return bool(getattr(get_settings(), "tcgcsv_enabled", True))

    async def get_market_price(self, query: str) -> MarketPrice | None:
        if not self.is_configured() or not query:
            return None
        try:
            await self._ensure_loaded()
        except Exception as exc:
            logger.debug("tcgcsv load failed: %s", exc)
            return None
        row = _cache.lookup(query)
        if not row:
            return None
        market = row.get("market") or row.get("mid") or row.get("low")
        if market is None:
            return None
        return MarketPrice(
            source="tcgcsv",
            market=float(market) if market is not None else None,
            low=row.get("low"),
            mid=row.get("mid"),
            high=row.get("high"),
            extras={"product_name": row.get("name"), "url": row.get("url")},
        )

    @staticmethod
    async def _ensure_loaded() -> None:
        if _cache.fresh():
            return
        async with _cache._lock:
            if _cache.fresh():
                return
            client = await get_http_client()
            by_name: dict[str, dict[str, Any]] = {}
            for tcg, cat_id in _CATEGORIES.items():
                try:
                    groups_resp = await client.get(
                        f"{_BASE}/{cat_id}/groups", timeout=30.0
                    )
                    groups_resp.raise_for_status()
                    groups = (groups_resp.json() or {}).get("results") or []
                except Exception as exc:
                    logger.debug("tcgcsv groups fetch (%s) failed: %s", tcg, exc)
                    continue

                for group in groups:
                    if len(by_name) >= _MAX_ROWS_PER_CATEGORY * len(_CATEGORIES):
                        break
                    gid = group.get("groupId")
                    if gid is None:
                        continue
                    try:
                        prod_csv = await client.get(
                            f"{_BASE}/{cat_id}/{gid}/products.csv", timeout=30.0
                        )
                        price_csv = await client.get(
                            f"{_BASE}/{cat_id}/{gid}/prices.csv", timeout=30.0
                        )
                        if prod_csv.status_code >= 400 or price_csv.status_code >= 400:
                            continue
                        _merge_group(by_name, prod_csv.text, price_csv.text)
                    except Exception as exc:
                        logger.debug(
                            "tcgcsv group %s/%s failed: %s", cat_id, gid, exc
                        )
                        continue

            _cache._by_name = by_name
            _cache._loaded_at = time.time()
            logger.info("tcgcsv loaded %d products", len(by_name))


def _merge_group(
    by_name: dict[str, dict[str, Any]], products_csv: str, prices_csv: str
) -> None:
    # products.csv columns: productId,name,cleanName,imageUrl,categoryId,
    # groupId,url,modifiedOn,...
    products: dict[str, dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(products_csv)):
        pid = row.get("productId")
        if not pid:
            continue
        products[pid] = {
            "name": (row.get("name") or "").strip(),
            "url": row.get("url") or "",
        }

    # prices.csv columns: productId,lowPrice,midPrice,highPrice,marketPrice,
    # directLowPrice,subTypeName
    for row in csv.DictReader(io.StringIO(prices_csv)):
        pid = row.get("productId")
        if not pid or pid not in products:
            continue
        prod = products[pid]
        name = prod["name"]
        if not name:
            continue
        key = name.lower()
        # Prefer "Normal" then "Holofoil" variants; first write wins for the key.
        if key in by_name:
            continue
        by_name[key] = {
            "name": name,
            "url": prod["url"],
            "low": _f(row.get("lowPrice")),
            "mid": _f(row.get("midPrice")),
            "high": _f(row.get("highPrice")),
            "market": _f(row.get("marketPrice")),
        }


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return round(f, 2) if f > 0 else None
    except (TypeError, ValueError):
        return None


__all__ = ["TcgCsvProvider"]
