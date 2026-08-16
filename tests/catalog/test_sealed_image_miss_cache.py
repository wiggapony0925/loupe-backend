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

from types import SimpleNamespace

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
    """Patch the resolver's LOOKUP, not the settings instance.

    The obvious version of this — `monkeypatch.setattr(get_settings(),
    "tcgcsv_enabled", True)` — passes locally and fails in CI, which is how it
    got written twice. `get_settings` is `lru_cache`d, and several tests call
    `reload_settings()` to clear that cache. Patching the instance binds the
    flag to whichever Settings object happened to be cached when the fixture
    ran; the moment anything clears the cache, `enrich_images` builds a fresh
    one with `tcgcsv_enabled` back at its `False` default, returns early, and
    the assertions fail with a bare `calls == 0` that says nothing about why.

    Whether that happens depends on collection ORDER, so a full local run and
    CI's `--ignore=tests/database` run disagree — the worst kind of red.

    The resolver does `from app.config import get_settings`, so the name in
    its module namespace is the one it actually calls. Patch that and the
    result no longer depends on cache state or on what ran first.
    """
    monkeypatch.setattr(
        resolver, "get_settings", lambda: SimpleNamespace(tcgcsv_enabled=True)
    )


@pytest.fixture
def _fresh_process(monkeypatch):
    """Pretend this process just started.

    THE CLOCK IS THE POINT. `time.monotonic()` counts from an arbitrary
    origin — in practice process or machine start — so on a laptop that has
    been up for days it returns six figures, and on a container that started
    ten seconds ago it returns 10. Every test in this file passed locally and
    failed in CI for exactly that reason, and the difference was a real bug
    (a missing entry defaulting to 0.0 read as "missed at time zero", which on
    a fresh container is inside the TTL, so enrichment never ran at all).

    Pinning the clock low makes the fresh-container case the DEFAULT the
    tests run under, rather than something only CI ever sees.
    """
    monkeypatch.setattr(resolver.time, "monotonic", lambda: 12.0)


@pytest.mark.asyncio
async def test_a_brand_new_process_still_enriches(monkeypatch, _enabled):
    """THE REGRESSION CI CAUGHT. A container that started seconds ago has a
    tiny monotonic clock; if "never asked" is stored as 0.0 it looks like a
    miss recorded at boot, and nothing is ever a target."""
    monkeypatch.setattr(resolver.time, "monotonic", lambda: 3.0)
    calls = 0

    async def _found(targets):
        nonlocal calls
        calls += 1
        for p in targets:
            p.image_url = "https://img.example.test/box.jpg"
        return True

    monkeypatch.setattr(resolver, "_resolve", _found)

    assert await resolver.enrich_images([_product()]) is True
    assert calls == 1, (
        "a freshly started process skipped enrichment entirely — an empty "
        "miss cache was read as 'everything was missed at time zero'"
    )


@pytest.mark.asyncio
async def test_a_timeout_is_not_retried_on_the_next_read(
    monkeypatch, _enabled, _fresh_process
):
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
    monkeypatch, _enabled, _fresh_process
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
    monkeypatch, _enabled, _fresh_process
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
async def test_a_different_product_is_still_attempted(
    monkeypatch, _enabled, _fresh_process
):
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
    monkeypatch, _enabled, _fresh_process
):
    async def _boom(_targets):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(resolver, "_resolve", _boom)
    product = _product()
    product.image_url = "https://img.example.test/have.jpg"

    assert await resolver.enrich_images([product]) is False
