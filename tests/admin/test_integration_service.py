"""Unit tests for the admin integrations (second-party services) report.

Probing is network I/O, so these stay offline: probe=False asserts the
catalog/config logic without any outbound calls.
"""

import pytest

from app.config import Settings
from app.integrations.registry import reset_registry
from app.services.admin import integration_service


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **over)


@pytest.fixture(autouse=True)
def _real_registry():
    """The report folds in the REAL provider catalog — clear the
    empty-registry override installed by conftest (probe=False keeps these
    tests offline)."""
    reset_registry()
    yield
    reset_registry()


@pytest.mark.asyncio
async def test_report_lists_registry_and_platform_services():
    report = await integration_service.report(probe=False, settings=_settings())
    ids = {i.id for i in report.integrations}
    # Registry-backed providers.
    assert {"pokemon_tcg", "ebay", "pricecharting", "stockx"} <= ids
    # Platform / keyless-catalog extras folded in.
    assert {"stripe", "resend", "anthropic", "sentry", "gcp", "scryfall"} <= ids
    assert report.probed is False
    for i in report.integrations:
        assert i.category and i.purpose
        assert i.status in {"live", "down", "ready", "unconfigured"}


@pytest.mark.asyncio
async def test_keyless_catalogs_are_always_configured():
    report = await integration_service.report(probe=False, settings=_settings())
    by_id = {i.id: i for i in report.integrations}
    assert by_id["scryfall"].configured is True
    assert by_id["ygoprodeck"].configured is True


@pytest.mark.asyncio
async def test_configured_reflects_settings():
    s = _settings(stripe_secret_key="sk_test_123", anthropic_api_key="")
    by_id = {
        i.id: i for i in (await integration_service.report(settings=s)).integrations
    }
    assert by_id["stripe"].configured is True
    assert by_id["stripe"].status == "ready"
    assert by_id["anthropic"].configured is False
    assert by_id["anthropic"].status == "unconfigured"


@pytest.mark.asyncio
async def test_unprobed_report_makes_no_status_live():
    # Without probing, nothing should be reported as "live".
    report = await integration_service.report(probe=False, settings=_settings())
    assert all(i.status != "live" for i in report.integrations)


@pytest.mark.asyncio
async def test_categories_are_grouped_in_order():
    report = await integration_service.report(probe=False, settings=_settings())
    ranks = [
        integration_service._category_rank(i.category) for i in report.integrations
    ]
    assert ranks == sorted(ranks)  # sorted by category order
