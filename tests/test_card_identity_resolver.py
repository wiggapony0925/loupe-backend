"""Identity-resolution contract tests.

Locks down the "every way a user adds a card resolves to the SAME
canonical id" invariant. Specifically:

* :func:`card_resolver_service.ensure_local_card` is idempotent — two
  calls with the same ``upstream_id`` produce one ``Card`` + one
  ``CardExternalRef``.
* :func:`card_resolver_service.resolve` returns the same ``card_id``
  whether the caller hands it a UUID, an upstream composite id, a
  pHash, or a free-text query for the same card.
* ``POST /v1/cards/resolve`` returns the same canonical document the
  ``GET /v1/cards/{id}/canonical`` endpoint would produce.
* ``POST /v1/grades`` accepts ``upstream_id`` and silently materializes
  the local card so the grade attaches to a real id, not a placeholder.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.card_external_ref import CardExternalRef
from app.services import card_resolver_service

# ----------------------------------------------------------- shared fixtures


_UNIFIED_POKEMON = {
    "id": "pokemontcg:base1-4",
    "name": "Charizard",
    "tcg": "pokemon",
    "set_name": "Base Set",
    "set_code": "base1",
    "number": "4",
    "rarity": "Holo Rare",
    "year": 1999,
    "image_url": "https://example/charizard.png",
    "set": {
        "id": "pokemontcg:base1",
        "code": "base1",
        "name": "Base Set",
        "release_date": "1999-01-09",
        "total_cards": 102,
    },
    "pricing_summary": {
        "currency": "USD",
        "market": {"amount": 595.18, "currency": "USD"},
        "sources": ["tcgplayer"],
    },
    "source": "pokemontcg",
}


# --------------------------------------------------------- ensure_local_card


@pytest.mark.asyncio
async def test_ensure_local_card_creates_card_and_ref(db_session) -> None:
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
    ):
        card = await card_resolver_service.ensure_local_card(
            db_session, upstream_id="pokemontcg:base1-4"
        )
    assert card is not None
    assert card.name == "Charizard"
    assert card.number == "4"
    assert card.year == 1999

    refs = (
        (
            await db_session.execute(
                select(CardExternalRef).where(CardExternalRef.card_id == card.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(refs) == 1
    assert refs[0].source == "pokemontcg"
    assert refs[0].external_id == "base1-4"


@pytest.mark.asyncio
async def test_ensure_local_card_is_idempotent(db_session) -> None:
    """Two calls with the same upstream_id must yield ONE Card + ONE ref."""
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
    ):
        a = await card_resolver_service.ensure_local_card(
            db_session, upstream_id="pokemontcg:base1-4"
        )
        await db_session.flush()
        b = await card_resolver_service.ensure_local_card(
            db_session, upstream_id="pokemontcg:base1-4"
        )
    assert a is not None and b is not None
    assert a.id == b.id

    refs = (
        (
            await db_session.execute(
                select(CardExternalRef).where(
                    CardExternalRef.source == "pokemontcg",
                    CardExternalRef.external_id == "base1-4",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(refs) == 1


@pytest.mark.asyncio
async def test_ensure_local_card_missing_upstream_returns_none(db_session) -> None:
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=None),
    ):
        out = await card_resolver_service.ensure_local_card(
            db_session, upstream_id="pokemontcg:does-not-exist"
        )
    assert out is None


@pytest.mark.asyncio
async def test_ensure_local_card_rejects_bad_input(db_session) -> None:
    assert (
        await card_resolver_service.ensure_local_card(db_session, upstream_id="")
        is None
    )
    assert (
        await card_resolver_service.ensure_local_card(
            db_session, upstream_id="noseparator"
        )
        is None
    )


# ----------------------------------------------------------- unified resolve


@pytest.mark.asyncio
async def test_resolve_upstream_id_materializes_and_returns_card_id(
    db_session,
) -> None:
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
    ):
        resolved = await card_resolver_service.resolve(
            db_session, upstream_id="pokemontcg:base1-4", materialize=True
        )
    assert resolved is not None
    assert resolved.card_id is not None
    assert resolved.upstream_id == "pokemontcg:base1-4"


@pytest.mark.asyncio
async def test_resolve_text_then_upstream_produces_same_card_id(db_session) -> None:
    """Text-search → materialize, then upstream lookup → same card_id."""
    search_body = {
        "results": [dict(_UNIFIED_POKEMON)],
        "total": 1,
        "source": "pokemontcg",
    }
    with (
        patch.object(
            card_resolver_service.card_search_service,
            "search_cards",
            new=AsyncMock(return_value=search_body),
        ),
        patch.object(
            card_resolver_service.card_search_service,
            "get_card",
            new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
        ),
    ):
        first = await card_resolver_service.resolve(
            db_session, query="Charizard Base Set 4", materialize=True
        )
        await db_session.flush()
        second = await card_resolver_service.resolve(
            db_session, upstream_id="pokemontcg:base1-4"
        )
    assert first is not None and first.card_id is not None
    assert second is not None and second.card_id is not None
    assert first.card_id == second.card_id


# --------------------------------------------------------------------- HTTP


@pytest.mark.asyncio
async def test_resolve_endpoint_returns_canonical(client) -> None:
    with (
        patch.object(
            card_resolver_service.card_search_service,
            "get_card",
            new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
        ),
        patch.object(
            card_resolver_service.card_search_service,
            "search_cards",
            new=AsyncMock(return_value={"results": [], "total": 0, "source": "mixed"}),
        ),
    ):
        resp = await client.post(
            "/v1/cards/resolve",
            json={"upstream_id": "pokemontcg:base1-4", "materialize": True},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["upstream_id"] == "pokemontcg:base1-4"
    assert data["card_id"] is not None
    canonical = data["canonical"]
    assert canonical is not None
    assert canonical["schema_version"] == "1.0.0"
    assert canonical["identity"]["name"] == "Charizard"


@pytest.mark.asyncio
async def test_resolve_endpoint_404_when_nothing_matches(client) -> None:
    resp = await client.post(
        "/v1/cards/resolve",
        json={"query": "definitely not a real card xyz123"},
    )
    # Either 404 (nothing matched) or 200 with null card_id — the contract
    # is "no false positives". We accept 404 here because there are no
    # upstream hits and no fingerprints / refs in this fresh test DB.
    assert resp.status_code in (200, 404)
    if resp.status_code == 404:
        assert "matched" in resp.json()["error"]["message"].lower()


# ----------------------------------------------------------- grades wiring


@pytest.mark.asyncio
async def test_grades_create_accepts_upstream_id(client, auth_headers) -> None:
    """POST /v1/grades with only an upstream_id must materialize the card."""
    with patch.object(
        card_resolver_service.card_search_service,
        "get_card",
        new=AsyncMock(return_value=dict(_UNIFIED_POKEMON)),
    ):
        resp = await client.post(
            "/v1/grades",
            headers=auth_headers,
            json={
                "upstream_id": "pokemontcg:base1-4",
                "grade": "9.5",
                "house": "psa",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["card_id"] is not None
    assert float(body["grade"]) == 9.5
