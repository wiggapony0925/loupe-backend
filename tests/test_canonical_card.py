"""Contract tests for the unified CanonicalCard composer + endpoint.

Locks down:

* The schema itself — required fields, defaults, "extra=forbid" boundaries.
* The composer's tolerance to upstream failures (every provider down
  must still produce a valid CanonicalCard built from catalog data
  alone, with the failures recorded in ``provenance.errors``).
* The composer's "happy path" — catalog + one quote + one comp + one
  listing all flow into the right canonical sections with provenance
  populated.
* The HTTP endpoint shape — ``GET /v1/cards/{id}/canonical`` returns
  a body that round-trips through :class:`CanonicalCard`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.base import Listing, MarketPrice, SoldComp
from app.schemas.canonical_card import (
    CANONICAL_CARD_VERSION,
    CanonicalCard,
    CanonicalIdentity,
    CanonicalProvenance,
)
from app.services import canonical_card_service

# --------------------------------------------------------------------- schema


def test_schema_version_constant() -> None:
    assert isinstance(CANONICAL_CARD_VERSION, str)
    assert CANONICAL_CARD_VERSION.count(".") == 2


def test_canonical_card_minimum_fields() -> None:
    card = CanonicalCard(
        identity=CanonicalIdentity(id="pokemontcg:base1-4", name="Charizard", tcg="pokemon"),
        provenance=CanonicalProvenance(composed_at="2026-01-01T00:00:00+00:00"),
    )
    assert card.schema_version == CANONICAL_CARD_VERSION
    assert card.pricing.quotes == []
    assert card.population.total == 0
    assert card.listings == []
    assert card.comps == []
    assert card.certs == []
    assert card.provenance.errors == []


def test_canonical_card_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        CanonicalCard(
            identity=CanonicalIdentity(id="x", name="x", tcg="pokemon"),
            provenance=CanonicalProvenance(composed_at="2026-01-01T00:00:00+00:00"),
            secret_field="nope",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------------- composer


def _stub_catalog_card() -> dict[str, Any]:
    return {
        "id": "pokemontcg:base1-4",
        "name": "Charizard",
        "tcg": "pokemon",
        "set_name": "Base Set",
        "set_code": "base1",
        "number": "4",
        "rarity": "Holo Rare",
        "year": 1999,
        "image_url": "https://example/charizard.png",
        "images": {
            "small": {"url": "https://example/s.png"},
            "large": {"url": "https://example/l.png"},
        },
        "set": {
            "id": "pokemontcg:base1",
            "code": "base1",
            "name": "Base Set",
            "release_date": "1999-01-09",
            "total_cards": 102,
        },
        "attributes": {"hp": 120, "types": ["Fire"]},
        "pricing_summary": {
            "currency": "USD",
            "market": {"amount": 300.0, "currency": "USD"},
            "low": {"amount": 250.0, "currency": "USD"},
            "high": {"amount": 400.0, "currency": "USD"},
            "as_of": "2026-05-19T00:00:00+00:00",
            "sample_size": 12,
            "sources": ["tcgplayer"],
        },
        "source": "pokemontcg",
        "tags": ["holo", "vintage"],
    }


@pytest.mark.asyncio
async def test_compose_card_not_found() -> None:
    with patch.object(
        canonical_card_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=None),
    ):
        out = await canonical_card_service.compose_canonical_card("nonexistent:1")
    assert out is None


@pytest.mark.asyncio
async def test_compose_catalog_only_all_providers_empty() -> None:
    """Every provider down → still returns a valid CanonicalCard from catalog."""
    with patch.object(
        canonical_card_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_stub_catalog_card()),
    ), patch.object(
        canonical_card_service.market_service,
        "get_card_market",
        new=AsyncMock(return_value=None),
    ), patch.object(
        canonical_card_service,
        "get_registry",
    ) as gr:
        registry = gr.return_value
        registry.fan_out_listings = AsyncMock(return_value=[])
        registry.fan_out_comps = AsyncMock(return_value=[])
        registry.fan_out_market_price = AsyncMock(return_value=[])
        registry.fan_out_population = AsyncMock(return_value=[])
        registry.get = lambda _id: None
        out = await canonical_card_service.compose_canonical_card("pokemontcg:base1-4")

    assert out is not None
    assert out.identity.name == "Charizard"
    assert out.identity.tcg == "pokemon"
    assert out.set is not None and out.set.code == "base1"
    assert out.images is not None and out.images.small is not None
    assert out.attributes is not None and out.attributes.hp == 120
    # Catalog pricing summary contributed one quote
    assert len(out.pricing.quotes) == 1
    assert out.pricing.quotes[0].source == "tcgplayer"
    assert out.pricing.consensus is not None
    assert out.pricing.consensus.amount == 300.0
    assert out.provenance.pricing_sources == ["tcgplayer"]
    assert out.provenance.errors == []


@pytest.mark.asyncio
async def test_compose_merges_provider_fanout() -> None:
    """Listings + comps + market quotes from providers flow into canonical sections."""
    market_quotes = [
        MarketPrice(source="tcgcsv", market=310.0, low=240.0, high=420.0),
        MarketPrice(source="justtcg", market=295.0),
    ]
    listings = [
        Listing(
            source="ebay",
            title="Charizard PSA 9",
            price=850.0,
            url="https://ebay.example/1",
            is_auction=False,
        )
    ]
    comps = [
        SoldComp(
            source="130point",
            title="Charizard PSA 10 sold",
            price=4200.0,
            sold_at="2026-05-01T00:00:00Z",
            grade="10",
            house="psa",
            url="https://130point.example/1",
        )
    ]

    with patch.object(
        canonical_card_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_stub_catalog_card()),
    ), patch.object(
        canonical_card_service.market_service,
        "get_card_market",
        new=AsyncMock(return_value=None),
    ), patch.object(
        canonical_card_service,
        "get_registry",
    ) as gr:
        registry = gr.return_value
        registry.fan_out_listings = AsyncMock(return_value=listings)
        registry.fan_out_comps = AsyncMock(return_value=comps)
        registry.fan_out_market_price = AsyncMock(return_value=market_quotes)
        registry.fan_out_population = AsyncMock(return_value=[])
        registry.get = lambda _id: None
        out = await canonical_card_service.compose_canonical_card("pokemontcg:base1-4")

    assert out is not None
    # 1 catalog + 2 provider quotes
    quote_sources = sorted(q.source for q in out.pricing.quotes)
    assert quote_sources == ["justtcg", "tcgcsv", "tcgplayer"]
    # consensus = median([300, 310, 295]) = 300
    assert out.pricing.consensus is not None
    assert out.pricing.consensus.amount == 300.0

    assert len(out.listings) == 1
    assert out.listings[0].source == "ebay"
    assert out.listings[0].price.amount == 850.0

    assert len(out.comps) == 1
    assert out.comps[0].house == "psa"
    assert out.comps[0].grade == "10"

    assert out.provenance.listings_sources == ["ebay"]
    assert out.provenance.comps_sources == ["130point"]
    assert out.provenance.pricing_sources == ["justtcg", "tcgcsv", "tcgplayer"]


@pytest.mark.asyncio
async def test_compose_psa_cert_id_triggers_cert_lookup(monkeypatch) -> None:
    """A ``psa:<cert>`` id should run the PSA provider's verify_cert."""
    monkeypatch.setenv("PSA_API_TOKEN", "tok")
    from app.config import reload_settings

    reload_settings()

    psa_payload = {
        "CertNumber": "12345",
        "Subject": "Charizard",
        "Year": "1999",
        "Brand": "Pokemon",
        "CardGrade": "PSA 10",
    }

    class _FakePsa:
        id = "psa"
        is_configured = staticmethod(lambda: True)

        async def verify_cert(self, cert_no):
            assert cert_no == "12345"
            return dict(psa_payload)

    with patch.object(
        canonical_card_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_stub_catalog_card()),
    ), patch.object(
        canonical_card_service.market_service,
        "get_card_market",
        new=AsyncMock(return_value=None),
    ), patch.object(
        canonical_card_service,
        "get_registry",
    ) as gr:
        registry = gr.return_value
        registry.fan_out_listings = AsyncMock(return_value=[])
        registry.fan_out_comps = AsyncMock(return_value=[])
        registry.fan_out_market_price = AsyncMock(return_value=[])
        registry.fan_out_population = AsyncMock(return_value=[])
        registry.get = lambda _id: _FakePsa() if _id == "psa" else None
        out = await canonical_card_service.compose_canonical_card("psa:12345")

    assert out is not None
    assert len(out.certs) == 1
    cert = out.certs[0]
    assert cert.house == "psa"
    assert cert.cert_number == "12345"
    assert cert.grade == "PSA 10"
    assert cert.subject == "Charizard"
    assert "psa" in out.provenance.cert_sources


@pytest.mark.asyncio
async def test_compose_swallows_provider_exception() -> None:
    """One provider raising must not fail the compose — it lands in errors."""

    async def boom(*_a, **_k):
        raise RuntimeError("ebay down")

    with patch.object(
        canonical_card_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=_stub_catalog_card()),
    ), patch.object(
        canonical_card_service.market_service,
        "get_card_market",
        new=AsyncMock(return_value=None),
    ), patch.object(
        canonical_card_service,
        "get_registry",
    ) as gr:
        registry = gr.return_value
        registry.fan_out_listings = boom
        registry.fan_out_comps = AsyncMock(return_value=[])
        registry.fan_out_market_price = AsyncMock(return_value=[])
        registry.fan_out_population = AsyncMock(return_value=[])
        registry.get = lambda _id: None
        out = await canonical_card_service.compose_canonical_card("pokemontcg:base1-4")

    assert out is not None
    assert any("listings" in e for e in out.provenance.errors)
    assert out.listings == []


# --------------------------------------------------------------------- endpoint


@pytest.mark.asyncio
async def test_canonical_endpoint_404(client) -> None:
    with patch.object(
        canonical_card_service,
        "compose_canonical_card",
        new=AsyncMock(return_value=None),
    ):
        resp = await client.get("/v1/cards/nonexistent:1/canonical")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_canonical_endpoint_round_trip(client) -> None:
    """The endpoint body must parse cleanly back into :class:`CanonicalCard`."""
    card = CanonicalCard(
        identity=CanonicalIdentity(
            id="pokemontcg:base1-4", name="Charizard", tcg="pokemon"
        ),
        provenance=CanonicalProvenance(composed_at="2026-05-19T00:00:00+00:00"),
    )
    with patch.object(
        canonical_card_service,
        "compose_canonical_card",
        new=AsyncMock(return_value=card),
    ):
        resp = await client.get("/v1/cards/pokemontcg:base1-4/canonical")

    assert resp.status_code == 200
    body = resp.json()
    # Universal envelope
    assert "data" in body
    payload = body["data"]
    # Round-trip
    parsed = CanonicalCard.model_validate(payload)
    assert parsed.identity.id == "pokemontcg:base1-4"
    assert parsed.schema_version == CANONICAL_CARD_VERSION
