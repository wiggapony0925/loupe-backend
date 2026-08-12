"""``/v1/cards/{id}/grade-summary``, ``/marketplace-prices`` and
``/valuation`` endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.base import Listing, SoldComp
from app.services.catalog import card_search_service
from app.services.market import (
    grade_summary_service,
    listings_service,
    marketplace_prices_service,
)
from app.services.market import (
    sold_comps_service as comps_service,
)
from tests.conftest import assert_envelope_error, assert_envelope_ok

_COMPOSITE = "pokemontcg:base1-4"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    async def _g(_k):
        return None

    async def _s(*_a, **_kw):
        return None

    for mod in (
        card_search_service,
        comps_service,
        listings_service,
        marketplace_prices_service,
    ):
        monkeypatch.setattr(mod, "_cache_get", _g, raising=False)
        monkeypatch.setattr(mod, "_cache_set", _s, raising=False)

    async def _no_tcgdex(*_a, **_kw):
        return []

    monkeypatch.setattr(
        marketplace_prices_service.TcgDexProvider,
        "get_market_prices_for_card_id",
        _no_tcgdex,
    )


def _patch_card(monkeypatch):
    async def fake(_id):
        return {
            "id": "base1-4",
            "name": "Charizard",
            "number": "4",
            "set": {"name": "Base Set"},
        }

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)


def _patch_comps(monkeypatch, comps):
    class _Reg:
        async def fan_out_comps(self, query, *, days, limit):
            return comps

    monkeypatch.setattr(comps_service, "get_registry", lambda: _Reg())


def _patch_listings(monkeypatch, listings):
    class _Reg:
        async def fan_out_listings(self, query, *, limit):
            return listings

        async def fan_out_market_price(self, query):
            return []

    monkeypatch.setattr(listings_service, "get_registry", lambda: _Reg())
    monkeypatch.setattr(marketplace_prices_service, "get_registry", lambda: _Reg())


# ----------------------------------------------------------- grade-summary


@pytest.mark.asyncio
async def test_grade_summary_pivots_by_grade(client, monkeypatch):
    _patch_card(monkeypatch)
    now = datetime.now(UTC)
    _patch_comps(
        monkeypatch,
        [
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2500.0,
                sold_at=_iso(now - timedelta(days=2)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2400.0,
                sold_at=_iso(now - timedelta(days=10)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 10",
                price=2000.0,
                sold_at=_iso(now - timedelta(days=45)),
                grade="10",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard PSA 9",
                price=800.0,
                sold_at=_iso(now - timedelta(days=5)),
                grade="9",
                house="psa",
            ),
            SoldComp(
                source="ebay",
                title="Charizard raw",
                price=300.0,
                sold_at=_iso(now - timedelta(days=3)),
                grade=None,
                house=None,
            ),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    body = assert_envelope_ok(resp)
    assert body["card_id"] == _COMPOSITE
    assert body["window_days"] == 30
    grades = body["grades"]
    # UNGRADED is pinned first.
    assert grades[0]["grade"] == "UNGRADED"
    keys = {g["grade"] for g in grades}
    assert {"UNGRADED", "PSA 10", "PSA 9"} <= keys
    psa10 = next(g for g in grades if g["grade"] == "PSA 10")
    assert psa10["sales_count"] == 2
    assert psa10["last_sale"]["amount"] == 2500.0
    # median(2500, 2400)=2450 vs baseline 2000 → +22.5%.
    assert psa10["delta_pct"] == 22.5


@pytest.mark.asyncio
async def test_grade_summary_empty_comps(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_comps(monkeypatch, [])
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    body = assert_envelope_ok(resp)
    assert body["grades"] == []


@pytest.mark.asyncio
async def test_grade_summary_404_on_unknown_card(client, monkeypatch):
    async def fake(_id):
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/grade-summary")
    assert resp.status_code == 404


# ------------------------------------------------------ marketplace-prices


@pytest.mark.asyncio
async def test_marketplace_prices_groups_by_source(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_listings(
        monkeypatch,
        [
            Listing(source="ebay", title="A", price=250.0, url="https://ebay/a"),
            Listing(source="ebay", title="B", price=199.0, url="https://ebay/b"),
            Listing(source="tcgplayer", title="C", price=210.0, url="https://tcg/c"),
        ],
    )
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    body = assert_envelope_ok(resp)
    providers = body["providers"]
    assert len(providers) == 2
    # Sorted by price ascending.
    assert providers[0]["source"] == "ebay"
    assert providers[0]["price"]["amount"] == 199.0
    assert providers[0]["url"] == "https://ebay/b"
    assert providers[0]["label"] == "eBay"
    assert providers[0]["search_url"].startswith("https://www.ebay.com/sch/")
    assert providers[1]["source"] == "tcgplayer"
    assert providers[1]["price"]["amount"] == 210.0


@pytest.mark.asyncio
async def test_marketplace_prices_empty(client, monkeypatch):
    _patch_card(monkeypatch)
    _patch_listings(monkeypatch, [])
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    body = assert_envelope_ok(resp)
    assert body["providers"] == []


@pytest.mark.asyncio
async def test_marketplace_prices_include_catalog_market_price(client, monkeypatch):
    async def fake(_id):
        return {
            "id": "pokemontcg:base1-4",
            "name": "Charizard",
            "number": "4",
            "set": {"name": "Base Set"},
            "source": "pokemontcg",
            "pricing_summary": {
                "currency": "EUR",
                "market": {"amount": 78.46, "currency": "EUR"},
                "low": {"amount": 25.0, "currency": "EUR"},
                "as_of": "2026/06/17",
                "sources": ["cardmarket"],
            },
        }

    monkeypatch.setattr(card_search_service, "get_card", fake)
    _patch_listings(monkeypatch, [])

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    body = assert_envelope_ok(resp)
    providers = body["providers"]
    assert len(providers) == 1
    assert providers[0]["source"] == "cardmarket"
    assert providers[0]["kind"] == "market_price"
    assert providers[0]["price"]["amount"] == 78.46
    assert providers[0]["price"]["currency"] == "EUR"
    assert [a["source"] for a in body["actions"]] == [
        "tcgplayer",
        "cardmarket",
        "pricecharting",
        "google_shopping",
    ]


@pytest.mark.asyncio
async def test_marketplace_prices_404_on_unknown_card(client, monkeypatch):
    async def fake(_id):
        return None

    monkeypatch.setattr(card_search_service.pokemon_tcg, "get_card", fake)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/marketplace-prices")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_listings_query_includes_nested_set_name(client, monkeypatch):
    _patch_card(monkeypatch)
    captured: dict[str, str] = {}

    class _Reg:
        async def fan_out_listings(self, query, *, limit):
            captured["query"] = query
            return [Listing(source="ebay", title=query, price=199.0)]

    monkeypatch.setattr(listings_service, "get_registry", lambda: _Reg())

    resp = await client.get(f"/v1/cards/{_COMPOSITE}/listings")
    body = assert_envelope_ok(resp)

    assert captured["query"] == "Charizard Base Set #4"
    assert body["query"] == "Charizard Base Set #4"


# ------------------------------------------------------------------ valuation


def _money(amount: float) -> dict[str, Any]:
    return {"amount": amount, "currency": "USD"}


def _patch_valuation_inputs(
    monkeypatch,
    *,
    catalog: float | None = None,
    providers: list[dict[str, Any]] | None = None,
    grades: list[dict[str, Any]] | None = None,
) -> None:
    """Pin the three signals valuation blends, so the arithmetic is exact.

    The composition of comps + listings + catalog is what this endpoint adds;
    each individual source already has its own tests above.
    """
    card: dict[str, Any] = {"id": "base1-4", "name": "Charizard", "number": "4"}
    if catalog is not None:
        card["pricing_summary"] = {"market": _money(catalog)}

    async def _card(_id):
        return card

    async def _marketplace(_id, **_kw):
        return {"providers": providers or []}

    async def _grade_summary(_id, **_kw):
        return {"grades": grades or []}

    monkeypatch.setattr(card_search_service, "get_card", _card)
    monkeypatch.setattr(
        marketplace_prices_service, "get_marketplace_prices_for_card", _marketplace
    )
    monkeypatch.setattr(
        grade_summary_service, "get_grade_summary_for_card", _grade_summary
    )


@pytest.mark.asyncio
async def test_valuation_blends_the_three_signals_by_weight(client, monkeypatch):
    """The Loupe Value is a weighted equilibrium, not a "latest number":
    actual sales (0.5) outrank asks (0.3) which outrank a catalog quote (0.2),
    because each is a progressively weaker proxy for a clearing price."""
    _patch_valuation_inputs(
        monkeypatch,
        catalog=100.0,
        providers=[{"source": "ebay", "price": _money(200.0)}],
        grades=[{"grade": "UNGRADED", "median_recent": 300.0}],
    )
    body = assert_envelope_ok(await client.get(f"/v1/cards/{_COMPOSITE}/valuation"))

    assert body["card_id"] == _COMPOSITE
    # 300*0.5 + 200*0.3 + 100*0.2 = 230
    assert body["fair_value"] == {"amount": 230.0, "currency": "USD"}
    assert body["confidence"] == 3
    assert body["signals"] == {
        "sold_comps": {"amount": 300.0, "currency": "USD"},
        "listings": {"amount": 200.0, "currency": "USD"},
        "catalog": {"amount": 100.0, "currency": "USD"},
    }


@pytest.mark.asyncio
async def test_valuation_renormalises_over_the_signals_it_actually_has(
    client, monkeypatch
):
    """A card with only a catalog quote still deserves a value — the weights
    are re-normalised over what's present rather than dragging the estimate
    toward zero for the missing sources."""
    _patch_valuation_inputs(monkeypatch, catalog=100.0)
    body = assert_envelope_ok(await client.get(f"/v1/cards/{_COMPOSITE}/valuation"))

    assert body["fair_value"] == {"amount": 100.0, "currency": "USD"}
    assert body["confidence"] == 1
    assert body["signals"]["sold_comps"] is None
    assert body["signals"]["listings"] is None


@pytest.mark.asyncio
async def test_valuation_reads_pricecharting_as_a_sold_signal_not_an_ask(
    client, monkeypatch
):
    """PriceCharting publishes realised sale prices, so counting it as a live
    listing would both understate the comps signal and double-count the ask
    side. It becomes the sold signal and is excluded from the listings median."""
    _patch_valuation_inputs(
        monkeypatch,
        catalog=100.0,
        providers=[
            {"source": "pricecharting", "price": _money(500.0)},
            {"source": "ebay", "price": _money(200.0)},
        ],
        grades=[{"grade": "UNGRADED", "median_recent": 300.0}],
    )
    body = assert_envelope_ok(await client.get(f"/v1/cards/{_COMPOSITE}/valuation"))

    assert body["signals"]["sold_comps"] == {"amount": 500.0, "currency": "USD"}
    assert body["signals"]["listings"] == {"amount": 200.0, "currency": "USD"}
    # 500*0.5 + 200*0.3 + 100*0.2 = 330
    assert body["fair_value"] == {"amount": 330.0, "currency": "USD"}


@pytest.mark.asyncio
async def test_valuation_reports_no_fair_value_rather_than_guessing(
    client, monkeypatch
):
    """With nothing to blend the honest answer is null. Inventing a number
    here would put a fabricated price on a collector's card."""
    _patch_valuation_inputs(monkeypatch)
    body = assert_envelope_ok(await client.get(f"/v1/cards/{_COMPOSITE}/valuation"))

    assert body["fair_value"] is None
    assert body["confidence"] == 0
    assert body["grades"] == []


@pytest.mark.asyncio
async def test_valuation_fills_grade_gaps_with_guide_prices_but_real_sales_win(
    client, monkeypatch
):
    """Sold comps are sparse for everything but chase cards, so PriceCharting's
    guide ladder backfills the grades nobody sold — flagged `is_guide` so the
    client can badge it, and never overwriting a real sale."""
    _patch_valuation_inputs(
        monkeypatch,
        providers=[
            {
                "source": "pricecharting",
                "price": _money(500.0),
                "grade_ladder": {"PSA 10": 1000.0, "PSA 9": 400.0},
            }
        ],
        grades=[{"grade": "PSA 10", "median_recent": 1200.0, "sales_count": 2}],
    )
    body = assert_envelope_ok(await client.get(f"/v1/cards/{_COMPOSITE}/valuation"))

    ladder = {row["grade"]: row for row in body["grades"]}
    assert set(ladder) == {"PSA 10", "PSA 9"}
    # The real sale keeps its own number and is not badged as a guide.
    assert ladder["PSA 10"]["median_recent"] == 1200.0
    assert ladder["PSA 10"].get("is_guide") is None
    assert ladder["PSA 9"]["is_guide"] is True
    assert ladder["PSA 9"]["median_recent"] == 400.0
    assert ladder["PSA 9"]["house"] == "psa"
    # Sorted most valuable first.
    assert [row["grade"] for row in body["grades"]] == ["PSA 10", "PSA 9"]


@pytest.mark.asyncio
async def test_valuation_404_on_unknown_card(client, monkeypatch):
    async def _missing(_id):
        return None

    monkeypatch.setattr(card_search_service, "get_card", _missing)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/valuation")
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_valuation_is_public(client, monkeypatch):
    """Card value is the hook that gets people into the product — it has to
    render for a signed-out visitor, so no Authorization header is sent."""
    _patch_valuation_inputs(monkeypatch, catalog=100.0)
    resp = await client.get(f"/v1/cards/{_COMPOSITE}/valuation")
    assert resp.status_code == 200
    assert assert_envelope_ok(resp)["fair_value"]["amount"] == 100.0
