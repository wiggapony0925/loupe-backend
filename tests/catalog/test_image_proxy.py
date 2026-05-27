"""Card image proxy — host allowlist + cache + upstream error paths."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.routers.catalog import image_proxy

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4"
    b"\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe"
    b"\xa3\x9cA\x07\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the per-process LRU between tests so cache state doesn't leak."""
    image_proxy._cache.clear()
    yield
    image_proxy._cache.clear()


@pytest.mark.asyncio
async def test_image_proxy_passes_through_allowed_host(client):
    url = "https://images.pokemontcg.io/base1/4.png"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200, content=_TINY_PNG, headers={"content-type": "image/png"}
            )
        )
        resp = await client.get("/v1/img", params={"u": url})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert "max-age=" in resp.headers["cache-control"]
    assert resp.headers["x-cache"] == "MISS"
    assert resp.content == _TINY_PNG


@pytest.mark.asyncio
async def test_image_proxy_serves_cache_on_second_hit(client):
    url = "https://cards.scryfall.io/normal/front/abc.jpg"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).mock(
            return_value=httpx.Response(
                200, content=_TINY_PNG, headers={"content-type": "image/jpeg"}
            )
        )
        first = await client.get("/v1/img", params={"u": url})
        second = await client.get("/v1/img", params={"u": url})

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    # Upstream hit exactly once across the two requests.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_image_proxy_rejects_disallowed_host(client):
    resp = await client.get(
        "/v1/img", params={"u": "https://evil.example.com/secret"}
    )
    # Envelope middleware wraps non-2xx as JSON envelope; just check status.
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_image_proxy_rejects_non_http_scheme(client):
    resp = await client.get(
        "/v1/img", params={"u": "file:///etc/passwd"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_image_proxy_502_on_upstream_failure(client):
    url = "https://images.ygoprodeck.com/cards/12345.jpg"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            side_effect=httpx.ConnectError("boom")
        )
        resp = await client.get("/v1/img", params={"u": url})
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_image_proxy_502_on_non_image_content_type(client):
    url = "https://images.pokemontcg.io/base1/5.png"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200, content=b"<html>not an image</html>",
                headers={"content-type": "text/html"},
            )
        )
        resp = await client.get("/v1/img", params={"u": url})
    assert resp.status_code == 502
