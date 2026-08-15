"""A sealed product that has no image upstream must be asked about once.

THE MEASUREMENT THAT MOTIVATED THIS. `/v1/sealed/search` enriches images on the
READ path, and 10 of the 31 sealed products in production have no resolvable
match. Every request therefore spent up to the full 9-second timeout re-asking
TCGplayer the same question and getting the same no.

Timing the home screen's launch fan-out against production:

    cold round      21,539ms      ← sealed/search alone was 21,536ms of it
    app config          8,532ms   ← queued behind it (containerConcurrency=4)
    announcement        8,629ms   ← same
    /health               384ms   ← unaffected, so it was not general slowness

One endpoint, on the screen that opens first, on every launch.
"""

from __future__ import annotations

import pytest

from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedProduct
from app.services.catalog import sealed_image_resolver as resolver


@pytest.fixture(autouse=True)
def _clean_cache():
    resolver.reset_miss_cache()
    yield
    resolver.reset_miss_cache()


def _product(name: str = "Surging Sparks") -> SealedProduct:
    return SealedProduct(
        name=f"{name} Booster Box",
        set_name=name,
        tcg=TcgEnum.pokemon,
        product_type=SealedProductTypeEnum.booster_box,
        image_url=None,
    )


@pytest.fixture
def _enabled(monkeypatch):
    """It is a plain field, not a property — set it on the cached instance."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "tcgcsv_enabled", True)


@pytest.mark.asyncio
async def test_a_timeout_is_not_retried_on_the_next_read(monkeypatch, _enabled):
    """The regression. This is the nine seconds, paid once per request."""
    calls = 0

    async def _slow(_targets):
        nonlocal calls
        calls += 1
        raise TimeoutError("upstream did not answer")

    monkeypatch.setattr(resolver, "_resolve", _slow)
    product = _product()

    assert await resolver.enrich_images([product]) is False
    assert calls == 1

    # Second read of the same catalog page — this used to pay the timeout again.
    assert await resolver.enrich_images([product]) is False
    assert calls == 1, (
        "the resolver was asked again after a timeout — every sealed search "
        "will keep paying the 9s timeout for a product that has no image"
    )


@pytest.mark.asyncio
async def test_a_successful_resolve_that_finds_nothing_is_also_remembered(
    monkeypatch, _enabled
):
    """Upstream answering "no match" is a real answer. Asking again cannot
    produce a different one until the catalog changes."""
    calls = 0

    async def _no_match(_targets):
        nonlocal calls
        calls += 1
        return False  # resolved fine, simply nothing to attach

    monkeypatch.setattr(resolver, "_resolve", _no_match)
    product = _product()

    await resolver.enrich_images([product])
    await resolver.enrich_images([product])
    assert calls == 1


@pytest.mark.asyncio
async def test_a_product_that_does_resolve_is_never_cached_as_a_miss(
    monkeypatch, _enabled
):
    """Success writes image_url to the row, which is its own permanent record —
    and the next product with the same key must not inherit a miss."""
    calls = 0

    async def _found(targets):
        nonlocal calls
        calls += 1
        for p in targets:
            p.image_url = "https://img.example.test/box.jpg"
        return True

    monkeypatch.setattr(resolver, "_resolve", _found)

    assert await resolver.enrich_images([_product()]) is True
    assert await resolver.enrich_images([_product()]) is True
    assert calls == 2, "a successful resolve was wrongly remembered as a miss"


@pytest.mark.asyncio
async def test_a_different_product_is_still_attempted(monkeypatch, _enabled):
    """The cache is keyed per product shape, not global — one dud must not
    suppress enrichment for the whole catalog."""
    seen: list[str] = []

    async def _resolve(targets):
        seen.extend(t.set_name or "" for t in targets)
        raise TimeoutError("nope")

    monkeypatch.setattr(resolver, "_resolve", _resolve)

    await resolver.enrich_images([_product("Surging Sparks")])
    await resolver.enrich_images([_product("Prismatic Evolutions")])

    assert seen == ["Surging Sparks", "Prismatic Evolutions"]


@pytest.mark.asyncio
async def test_products_that_already_have_an_image_are_never_targets(
    monkeypatch, _enabled
):
    async def _boom(_targets):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(resolver, "_resolve", _boom)
    product = _product()
    product.image_url = "https://img.example.test/have.jpg"

    assert await resolver.enrich_images([product]) is False
