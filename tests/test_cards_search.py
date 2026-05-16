"""Card catalog search tests (uses factory-created seed rows)."""

import pytest

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum


@pytest.mark.asyncio
async def test_search_cards_returns_pagination(client, db_session):
    cset = CardSet(tcg=TcgEnum.pokemon, name="Base Set", code="BASE")
    db_session.add(cset)
    await db_session.flush()
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Charizard"))
    db_session.add(Card(set_id=cset.id, tcg=TcgEnum.pokemon, name="Pikachu"))
    await db_session.commit()

    resp = await client.get("/v1/cards", params={"q": "char", "tcg": "pokemon"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 1
    assert any(item["name"].lower().startswith("char") for item in body["items"])


@pytest.mark.asyncio
async def test_list_sets(client, db_session):
    db_session.add(CardSet(tcg=TcgEnum.magic, name="Alpha", code="LEA"))
    await db_session.commit()
    resp = await client.get("/v1/sets", params={"tcg": "magic"})
    assert resp.status_code == 200
    assert resp.json()["total"] >= 1
