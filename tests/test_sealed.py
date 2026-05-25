"""End-to-end tests for `/v1/sealed` + `/v1/sealed-holdings`."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.enums import SealedProductTypeEnum, TcgEnum
from app.models.sealed import SealedHolding, SealedProduct
from tests.conftest import assert_envelope_error, assert_envelope_ok


async def _make_product(
    db_session,
    *,
    name: str = "Scarlet & Violet — 151 Booster Box",
    product_type: SealedProductTypeEnum = SealedProductTypeEnum.booster_box,
    tcg: TcgEnum = TcgEnum.pokemon,
    set_name: str | None = "Scarlet & Violet — 151",
    msrp: Decimal | None = Decimal("161.64"),
) -> SealedProduct:
    row = SealedProduct(
        tcg=tcg,
        product_type=product_type,
        name=name,
        set_name=set_name,
        msrp_usd=msrp,
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
    resp = await client.post(
        "/v1/sealed-holdings", json=payload, headers=auth_headers
    )
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
    holding = SealedHolding(
        user_id=created_user.id, product_id=product.id, quantity=1
    )
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
