"""130point scraper — HTML parsing + cache + graceful failure."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.integrations.base import close_http_client
from app.integrations.point130 import Point130Provider

_HTML = """
<html><body><table>
<tr><td><a href="https://130point.com/1">Charizard PSA 10</a></td>
    <td>$2,499.99</td><td>2025-02-01</td></tr>
<tr><td><a href="https://130point.com/2">Charizard PSA 9</a></td>
    <td>$899.50</td><td>2025-01-15</td></tr>
<tr><td>No price here</td><td>n/a</td><td>n/a</td></tr>
</table></body></html>
"""


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_http_client()


@pytest.mark.asyncio
async def test_point130_parse(monkeypatch):
    # Bypass caches.
    async def _miss(_k):
        return None

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: _FakeRedis())
    p = Point130Provider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://130point.com/sales").mock(
            return_value=httpx.Response(200, text=_HTML)
        )
        out = await p.search_sold_comps("charizard", limit=10)
    assert len(out) == 2
    assert out[0].price == 2499.99
    assert out[0].house == "psa"


class _FakeRedis:
    async def get(self, _k):
        return None

    async def setex(self, *_a, **_kw):
        return None


@pytest.mark.asyncio
async def test_point130_scrape_failure_returns_empty(monkeypatch):
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: _FakeRedis())
    p = Point130Provider()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://130point.com/sales").mock(
            return_value=httpx.Response(500)
        )
        out = await p.search_sold_comps("anything", limit=10)
    assert out == []
