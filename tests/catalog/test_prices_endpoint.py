"""Tests for `GET /v1/prices` — the paginated price-snapshot history."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.enums import GradeHouseEnum, PriceSourceEnum
from app.models.price import PriceSnapshot
from tests.conftest import (
    assert_envelope_error,
    assert_envelope_ok,
    envelope_pagination,
)
from tests.factories import make_card


async def _snapshot(
    db_session,
    card,
    *,
    price: str,
    sale_date: date | None = None,
    house: GradeHouseEnum = GradeHouseEnum.psa,
    grade: str = "10.0",
    source: PriceSourceEnum = PriceSourceEnum.ebay,
) -> PriceSnapshot:
    row = PriceSnapshot(
        card_id=card.id,
        house=house,
        grade=Decimal(grade),
        source=source,
        price_usd=Decimal(price),
        sale_date=sale_date,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_snapshots_come_back_newest_sale_first(client, db_session):
    """The price list is a history feed: the most recent sale is the one a
    collector reads first, so ordering is by sale date descending."""
    card = await make_card(db_session)
    await _snapshot(db_session, card, price="100.00", sale_date=date(2026, 1, 1))
    await _snapshot(db_session, card, price="300.00", sale_date=date(2026, 3, 1))
    await _snapshot(db_session, card, price="200.00", sale_date=date(2026, 2, 1))

    resp = await client.get("/v1/prices", params={"card_id": str(card.id)})
    items = assert_envelope_ok(resp)
    assert [i["price_usd"] for i in items] == ["300.00", "200.00", "100.00"]
    assert all(i["card_id"] == str(card.id) for i in items)
    assert envelope_pagination(resp)["total"] == 3


@pytest.mark.asyncio
async def test_prices_are_public(client, db_session):
    """Price history is catalog data, not user data — no auth header here."""
    card = await make_card(db_session)
    await _snapshot(db_session, card, price="12.00")
    resp = await client.get("/v1/prices", params={"card_id": str(card.id)})
    assert len(assert_envelope_ok(resp)) == 1


@pytest.mark.asyncio
async def test_snapshots_are_scoped_to_the_requested_card(client, db_session):
    """A shared price table means the card filter is the whole contract — a
    leak here would show one card's comps on another card's detail sheet."""
    card = await make_card(db_session, name="Wanted")
    other = await make_card(db_session, name="Unwanted")
    await _snapshot(db_session, card, price="50.00")
    await _snapshot(db_session, other, price="99.00")

    items = assert_envelope_ok(
        await client.get("/v1/prices", params={"card_id": str(card.id)})
    )
    assert [i["price_usd"] for i in items] == ["50.00"]


@pytest.mark.asyncio
async def test_house_grade_and_source_filters_narrow_the_history(client, db_session):
    """Each filter maps to a real question — "what do PSA 10s go for on
    eBay?" — so they have to compose rather than override one another."""
    card = await make_card(db_session)
    await _snapshot(db_session, card, price="1000.00")  # psa 10 ebay
    await _snapshot(db_session, card, price="400.00", grade="9.0")
    await _snapshot(db_session, card, price="900.00", house=GradeHouseEnum.bgs)
    await _snapshot(db_session, card, price="950.00", source=PriceSourceEnum.tcgplayer)

    base = {"card_id": str(card.id)}
    psa = assert_envelope_ok(
        await client.get("/v1/prices", params={**base, "house": "psa"})
    )
    assert {i["price_usd"] for i in psa} == {"1000.00", "400.00", "950.00"}

    tens = assert_envelope_ok(
        await client.get("/v1/prices", params={**base, "grade": "10"})
    )
    assert {i["price_usd"] for i in tens} == {"1000.00", "900.00", "950.00"}

    combined = assert_envelope_ok(
        await client.get(
            "/v1/prices", params={**base, "house": "psa", "source": "ebay"}
        )
    )
    assert {i["price_usd"] for i in combined} == {"1000.00", "400.00"}


@pytest.mark.asyncio
async def test_pagination_walks_the_history_without_repeating_rows(client, db_session):
    """`total` stays the full count while `items` shrinks to the page, so the
    client can render "showing 2 of 3" without a second round-trip."""
    card = await make_card(db_session)
    for i, day in enumerate((1, 2, 3), start=1):
        await _snapshot(
            db_session, card, price=f"{i}0.00", sale_date=date(2026, 1, day)
        )

    base = {"card_id": str(card.id), "page_size": 2}
    first = await client.get("/v1/prices", params={**base, "page": 1})
    page_one = assert_envelope_ok(first)
    assert [i["price_usd"] for i in page_one] == ["30.00", "20.00"]
    assert envelope_pagination(first)["total"] == 3

    second = await client.get("/v1/prices", params={**base, "page": 2})
    page_two = assert_envelope_ok(second)
    assert [i["price_usd"] for i in page_two] == ["10.00"]
    assert envelope_pagination(second)["total"] == 3


@pytest.mark.asyncio
async def test_card_id_is_required(client):
    """Without a card there is no meaningful "all prices" answer, so the
    query param is mandatory rather than defaulting to a full-table scan."""
    assert_envelope_error(await client.get("/v1/prices"), expected_status=422)


@pytest.mark.asyncio
async def test_unknown_card_404s_rather_than_looking_price_less(client):
    """ "No prices yet" and "no such card" are different answers, so an id that
    matches no card 404s instead of rendering as an empty history — the same
    contract every sibling card read (grade-summary, valuation, canonical)
    already keeps."""
    assert_envelope_error(
        await client.get("/v1/prices", params={"card_id": str(uuid.uuid4())}),
        expected_status=404,
    )


@pytest.mark.asyncio
async def test_a_known_card_with_no_snapshots_is_an_empty_page(client, db_session):
    """The 404 above is about the card, not the history: a real card nobody has
    priced yet still answers 200 with an empty page."""
    card = await make_card(db_session)
    resp = await client.get("/v1/prices", params={"card_id": str(card.id)})
    assert assert_envelope_ok(resp) == []
    assert envelope_pagination(resp)["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page_size": 0},
        {"page_size": 101},
        {"grade": "11"},
        {"grade": "-1"},
        {"house": "nonsense"},
        {"source": "nonsense"},
    ],
)
async def test_out_of_range_query_params_are_rejected(client, db_session, params):
    """Bounds are enforced at the edge so a client can't ask for page_size=10000
    and turn a detail sheet into a table scan."""
    card = await make_card(db_session)
    resp = await client.get("/v1/prices", params={"card_id": str(card.id), **params})
    assert_envelope_error(resp, expected_status=422)
