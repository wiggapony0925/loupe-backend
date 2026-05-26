"""130point.com sold-comp scraper (no API, polite single concurrent request).

Always 'configured' (no key needed) but rate-limited to ~1 req/sec via
process-wide semaphore + Redis-cached 24h. Falls back silently on parse
failures.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.platform.redis_client import get_redis
from app.integrations.base import BaseProvider, SoldComp, parse_grade
from app.utils.logger import get_logger

logger = get_logger("integrations.point130")

_BASE = "https://130point.com/sales/"
_RATE_LIMIT = asyncio.Semaphore(1)
_LAST_REQ_AT: list[float] = [0.0]
_MIN_INTERVAL_S = 1.0
_CACHE_PREFIX = "loupe:point130:sold"
_CACHE_TTL = 86400  # 24h


class Point130Provider(BaseProvider):
    id = "130point"
    name = "130point"

    def is_configured(self) -> bool:
        # Public site; treat as always available.
        return True

    async def search_sold_comps(
        self, query: str, *, days: int = 90, limit: int = 50
    ) -> list[SoldComp]:
        if not query:
            return []
        cache_key = f"{_CACHE_PREFIX}:{query.lower()}:{days}:{limit}"
        try:
            r = await get_redis()
            cached = await r.get(cache_key)
            if cached:
                payload = json.loads(cached)
                return [SoldComp(**row) for row in payload]
        except Exception as exc:
            logger.debug("point130 cache read failed: %s", exc)

        rows = await self._scrape(query, limit=limit)

        try:
            r = await get_redis()
            await r.setex(
                cache_key,
                _CACHE_TTL,
                json.dumps([row.to_dict() for row in rows]),
            )
        except Exception as exc:
            logger.debug("point130 cache write failed: %s", exc)
        return rows

    async def _scrape(self, query: str, *, limit: int) -> list[SoldComp]:
        url = f"{_BASE}?q={quote(query)}"
        async with _RATE_LIMIT:
            # Politeness: ensure ~1s between outbound requests.
            import time

            wait = _MIN_INTERVAL_S - (time.time() - _LAST_REQ_AT[0])
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._call_with_retry("GET", url, retries=1)
                _LAST_REQ_AT[0] = time.time()
                if resp is None or resp.status_code >= 400:
                    return []
                return self._parse(resp.text, limit=limit)
            except Exception as exc:
                logger.warning("point130 scrape failed: %s", exc)
                return []

    @staticmethod
    def _parse(html: str, *, limit: int) -> list[SoldComp]:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")
        out: list[SoldComp] = []
        # 130point renders results in a <table> with <tr class="sale-row">
        # or generic <tr>; be defensive.
        for row in soup.select("tr")[: limit * 4]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            text = " ".join(c.get_text(" ", strip=True) for c in cells)
            price_match = re.search(r"\$([0-9][0-9,]*\.?\d{0,2})", text)
            if not price_match:
                continue
            try:
                price = float(price_match.group(1).replace(",", ""))
            except ValueError:
                continue
            if price <= 0:
                continue
            link = row.find("a", href=True)
            href: str | None = None
            if link is not None:
                href_val = link.get("href")
                if isinstance(href_val, str):
                    href = href_val
            title = (link.get_text(" ", strip=True) if link else "") or text[:120]
            house, grade = parse_grade(title)
            out.append(
                SoldComp(
                    source="130point",
                    title=title,
                    price=round(price, 2),
                    sold_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    grade=grade,
                    house=house,
                    url=href,
                )
            )
            if len(out) >= limit:
                break
        return out


__all__ = ["Point130Provider"]
