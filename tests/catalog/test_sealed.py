"""End-to-end tests for `/v1/sealed` + `/v1/sealed-holdings`."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.integrations import pricecharting
from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedHolding, SealedProduct
from app.services.catalog import sealed_image_resolver
from app.utils.time import utcnow
from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _make_product(
    db_session,
    *,
    name: str = "Scarlet & Violet — 151 Booster Box",
    product_type: SealedProductTypeEnum = SealedProductTypeEnum.booster_box,
    tcg: TcgEnum = TcgEnum.pokemon,
    set_name: str | None = "Scarlet & Violet — 151",
    msrp: Decimal | None = Decimal("161.64"),
    release_date: date | None = None,
    price_history: list | None = None,
) -> SealedProduct:
    row = SealedProduct(
        tcg=tcg,
        product_type=product_type,
        name=name,
        set_name=set_name,
        msrp_usd=msrp,
        release_date=release_date,
        price_history=price_history,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_catalog_search_filters_by_query_and_type(client, db_session):
    await _make_product(db_session, name="151 Booster Box")
    await _make_product(
        db_session,
        name="151 Elite Trainer Box",
        product_type=SealedProductTypeEnum.etb,
        msrp=Decimal("49.99"),
    )
    await _make_product(
        db_session,
        name="Crown Zenith Booster Box",
        set_name="Crown Zenith",
    )

    resp = await client.get("/v1/sealed/search", params={"q": "151"})
    rows = assert_envelope_ok(resp)
    assert len(rows) == 2
    assert all("151" in r["name"] for r in rows)

    resp = await client.get(
        "/v1/sealed/search", params={"q": "151", "product_type": "etb"}
    )
    rows = assert_envelope_ok(resp)
    assert len(rows) == 1
    assert rows[0]["product_type"] == "etb"


@pytest.mark.asyncio
async def test_catalog_detail_returns_product(client, db_session):
    product = await _make_product(db_session)
    resp = await client.get(f"/v1/sealed/{product.id}")
    data = assert_envelope_ok(resp)
    assert data["name"] == product.name
    assert data["tcg"] == "pokemon"


@pytest.mark.asyncio
async def test_catalog_detail_unknown_returns_404(client):
    resp = await client.get(f"/v1/sealed/{uuid.uuid4()}")
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_create_list_delete_holding_roundtrip(
    client, db_session, created_user, auth_headers
):
    product = await _make_product(db_session)

    payload = {
        "product_id": str(product.id),
        "quantity": 2,
        "purchase_price_usd": "150.00",
        "purchase_date": "2024-01-15",
        "notes": "Costco snipe",
    }
    resp = await client.post("/v1/sealed-holdings", json=payload, headers=auth_headers)
    data = assert_envelope_ok(resp, expected_status=201)
    holding_id = data["id"]
    assert data["product_id"] == str(product.id)
    assert data["quantity"] == 2
    assert data["product_name"] == product.name
    assert data["product_type"] == "booster_box"

    resp = await client.get("/v1/sealed-holdings", headers=auth_headers)
    rows = assert_envelope_ok(resp)
    assert len(rows) == 1
    assert rows[0]["id"] == holding_id

    resp = await client.delete(
        f"/v1/sealed-holdings/{holding_id}", headers=auth_headers
    )
    assert resp.status_code == 204

    resp = await client.get("/v1/sealed-holdings", headers=auth_headers)
    assert assert_envelope_ok(resp) == []


@pytest.mark.asyncio
async def test_update_holding_changes_quantity_and_marks_opened(
    client, db_session, created_user, auth_headers
):
    product = await _make_product(db_session)
    holding = SealedHolding(user_id=created_user.id, product_id=product.id, quantity=1)
    db_session.add(holding)
    await db_session.commit()
    await db_session.refresh(holding)

    resp = await client.patch(
        f"/v1/sealed-holdings/{holding.id}",
        json={"quantity": 3, "opened_at": "2024-06-01T12:00:00Z"},
        headers=auth_headers,
    )
    data = assert_envelope_ok(resp)
    assert data["quantity"] == 3
    assert data["opened_at"] is not None


@pytest.mark.asyncio
async def test_update_holding_can_clear_nullable_value_fields(
    client, db_session, created_user, auth_headers
):
    product = await _make_product(db_session)
    holding = SealedHolding(
        user_id=created_user.id,
        product_id=product.id,
        quantity=2,
        purchase_price_usd=Decimal("150.00"),
        estimated_value_usd=Decimal("200.00"),
        notes="shelf copy",
    )
    db_session.add(holding)
    await db_session.commit()
    await db_session.refresh(holding)

    data = assert_envelope_ok(
        await client.patch(
            f"/v1/sealed-holdings/{holding.id}",
            json={
                "purchase_price_usd": None,
                "estimated_value_usd": None,
                "notes": None,
            },
            headers=auth_headers,
        )
    )
    assert data["purchase_price_usd"] is None
    assert data["estimated_value_usd"] is None
    assert data["notes"] is None


@pytest.mark.asyncio
async def test_holdings_require_auth(client):
    resp = await client.get("/v1/sealed-holdings")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_create_holding_rejects_unknown_product(client, auth_headers):
    resp = await client.post(
        "/v1/sealed-holdings",
        json={"product_id": str(uuid.uuid4()), "quantity": 1},
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_purchase_date_future_rejected(client, db_session, auth_headers):
    product = await _make_product(db_session)
    resp = await client.post(
        "/v1/sealed-holdings",
        json={
            "product_id": str(product.id),
            "quantity": 1,
            "purchase_date": "2999-01-01",
        },
        headers=auth_headers,
    )
    assert_envelope_error(resp, expected_status=422)


# ── `/v1/sealed/{product_id}/market` ──────────────────────────────────────


@pytest.fixture
def sealed_market(monkeypatch):
    """Control both upstream quote sources for `/market`.

    Neither TCGplayer (via TCGCSV) nor PriceCharting goes through the provider
    registry the conftest empties, so without this the endpoint would reach
    the real network whenever a developer's `.env` carries live keys.
    """

    state: dict[str, dict | None] = {"tcgplayer": None, "pricecharting": None}

    async def _tcgplayer(_product):
        return state["tcgplayer"]

    async def _pricecharting(_product):
        return state["pricecharting"]

    monkeypatch.setattr(sealed_image_resolver, "resolve_market", _tcgplayer)
    monkeypatch.setattr(pricecharting, "resolve_sealed_market", _pricecharting)
    return state


@pytest.mark.asyncio
async def test_market_snapshot_prefers_the_tcgplayer_quote(
    client, db_session, sealed_market
):
    """TCGplayer is the only source that gives low/mid/high, so it owns the
    headline number whenever it resolves — a richer quote beats a bare one."""
    product = await _make_product(db_session)
    sealed_market["tcgplayer"] = {
        "low": 150.0,
        "mid": 175.0,
        "high": 210.0,
        "market": 180.0,
        "currency": "USD",
        "source": "tcgplayer",
        "marketplace_url": "https://tcgplayer.example/151-booster-box",
    }
    sealed_market["pricecharting"] = {"market": 999.0, "source": "pricecharting"}

    body = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    assert body["product_id"] == str(product.id)
    assert body["market"] == 180.0
    assert body["source"] == "tcgplayer"
    assert (body["low"], body["mid"], body["high"]) == (150.0, 175.0, 210.0)
    assert body["marketplace_url"] == "https://tcgplayer.example/151-booster-box"
    assert body["msrp_usd"] == "161.64"


@pytest.mark.asyncio
async def test_market_falls_back_to_pricecharting_when_tcgplayer_misses(
    client, db_session, sealed_market
):
    """TCGplayer matches sealed SKUs by fuzzy catalog name and often finds
    nothing; PriceCharting indexes them cleanly, so it fills the gap rather
    than leaving the product priceless."""
    product = await _make_product(db_session)
    sealed_market["tcgplayer"] = None
    sealed_market["pricecharting"] = {"market": 142.5, "source": "pricecharting"}

    body = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    assert body["market"] == 142.5
    assert body["source"] == "pricecharting"
    assert body["low"] is None and body["high"] is None


@pytest.mark.asyncio
async def test_market_is_all_nulls_when_no_source_resolves(
    client, db_session, sealed_market
):
    """A product nobody quotes still has to render — the client shows "no
    market yet" off null prices instead of receiving an error."""
    product = await _make_product(db_session, msrp=None)
    body = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    assert body["market"] is None
    assert body["source"] is None
    assert body["msrp_usd"] is None
    assert body["points"] == []


@pytest.mark.asyncio
async def test_value_line_anchors_at_msrp_on_release_then_todays_market(
    client, db_session, sealed_market
):
    """Sealed product has no stored daily history, so the chart is honest
    about what it knows: what it cost at launch, and what it costs now."""
    product = await _make_product(db_session, release_date=date(2023, 9, 22))
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}

    body = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    today = utcnow().date().isoformat()
    assert body["points"] == [
        {"ts": "2023-09-22", "price": 161.64},
        {"ts": today, "price": 180.0},
    ]


@pytest.mark.asyncio
async def test_the_value_lines_last_point_always_matches_the_headline(
    client, db_session, sealed_market
):
    """The chart and the big number above it are one reading of the market, so
    they cannot disagree: today gets exactly one point, and that point carries
    the same quote the `market` field reports."""
    product = await _make_product(db_session, msrp=None)
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}

    first = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    sealed_market["tcgplayer"] = {"market": 195.0, "source": "tcgplayer"}
    second = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))

    today = utcnow().date().isoformat()
    assert first["points"] == [{"ts": today, "price": 180.0}]
    assert second["market"] == 195.0
    assert second["points"] == [{"ts": today, "price": 195.0}]
    assert [p["ts"] for p in second["points"]].count(today) == 1


@pytest.mark.asyncio
async def test_an_anonymous_market_read_never_writes_to_the_catalog(
    client, db_session, sealed_market
):
    """`/market` is an unauthenticated public read. Recording an observation is
    a commit against a shared catalog row, so an anonymous caller must not be
    able to trigger one — otherwise a loop over a public GET is an unbounded
    write amplifier."""
    product = await _make_product(db_session, msrp=None)
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}

    for _ in range(3):
        assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))

    db_session.expunge_all()
    stored = await db_session.get(SealedProduct, product.id)
    assert not stored.price_history, "an anonymous read persisted a price observation"


@pytest.mark.asyncio
async def test_a_signed_in_read_records_todays_observation(
    client, db_session, sealed_market, auth_headers
):
    """The value line still has to grow, so the observation capture survives —
    it is just gated on a signed-in caller, and it keeps the latest quote of
    the day rather than the first."""
    product = await _make_product(db_session, msrp=None)
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}
    assert_envelope_ok(
        await client.get(f"/v1/sealed/{product.id}/market", headers=auth_headers)
    )

    db_session.expunge_all()
    stored = await db_session.get(SealedProduct, product.id)
    today = utcnow().date().isoformat()
    assert stored.price_history == [{"d": today, "p": 180.0}]

    sealed_market["tcgplayer"] = {"market": 195.0, "source": "tcgplayer"}
    body = assert_envelope_ok(
        await client.get(f"/v1/sealed/{product.id}/market", headers=auth_headers)
    )
    assert body["points"] == [{"ts": today, "price": 195.0}]

    db_session.expunge_all()
    stored = await db_session.get(SealedProduct, product.id)
    assert stored.price_history == [{"d": today, "p": 195.0}]


@pytest.mark.asyncio
async def test_value_line_keeps_previously_recorded_days(
    client, db_session, sealed_market
):
    """Yesterday's observations are the whole point of accumulating history —
    they must survive today's read and stay in date order."""
    product = await _make_product(
        db_session,
        msrp=None,
        price_history=[
            {"d": "2026-01-02", "p": 170.0},
            {"d": "2026-01-01", "p": 160.0},
        ],
    )
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}

    body = assert_envelope_ok(await client.get(f"/v1/sealed/{product.id}/market"))
    today = utcnow().date().isoformat()
    assert [p["ts"] for p in body["points"]] == ["2026-01-01", "2026-01-02", today]
    assert [p["price"] for p in body["points"]] == [160.0, 170.0, 180.0]


@pytest.mark.asyncio
async def test_market_404_for_unknown_product(client, sealed_market):
    resp = await client.get(f"/v1/sealed/{uuid.uuid4()}/market")
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_market_is_public(client, db_session, sealed_market):
    """Sealed catalog browsing works signed out, so pricing does too."""
    product = await _make_product(db_session)
    sealed_market["tcgplayer"] = {"market": 180.0, "source": "tcgplayer"}
    resp = await client.get(f"/v1/sealed/{product.id}/market")
    assert resp.status_code == 200
    assert assert_envelope_ok(resp)["market"] == 180.0
