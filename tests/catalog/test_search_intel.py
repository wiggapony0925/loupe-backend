"""Deterministic query understanding (search_intel) — the zero-AI parser.

Google-style free text → structured intent. These tests pin the vocabulary:
price bands, sort phrases, game aliases, rarity vocabulary, set phrases and
years all parse out; plain card names round-trip UNTOUCHED (the fast search
path must stay valid); ambiguous words that appear in real card names
("Rare Candy", "Charizard ex") are deliberately left alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.catalog import card_search_service, search_intel
from app.services.catalog.search_intel import (
    QueryIntent,
    filter_cards,
    parse_query,
    sort_cards,
)


def _card(
    id_: str,
    price: float | None = None,
    rarity: str | None = None,
    set_name: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    ps = None if price is None else {"market": {"amount": price, "currency": "USD"}}
    return {
        "id": id_,
        "name": id_,
        "rarity": rarity,
        "set_name": set_name,
        "year": year,
        "pricing_summary": ps,
    }


# ── Plain queries stay plain ──


def test_plain_query_round_trips_untouched() -> None:
    intent = parse_query("charizard vmax")
    assert intent.text == "charizard vmax"
    assert intent.plain and not intent.has_signal


def test_ambiguous_card_name_words_are_not_stripped() -> None:
    # "rare", "ex", "v" occur inside real card names — the conservative
    # vocabulary must not eat them.
    assert parse_query("rare candy").text == "rare candy"
    assert parse_query("charizard ex").text == "charizard ex"


def test_empty_query() -> None:
    intent = parse_query("   ")
    assert intent == QueryIntent()


# ── Individual modifiers ──


def test_price_under_and_over() -> None:
    intent = parse_query("charizard under $50")
    assert intent.text == "charizard"
    assert intent.price_max == 50 and intent.price_min is None
    assert "Under $50" in intent.chips

    intent = parse_query("over 100 umbreon")
    assert intent.text == "umbreon"
    assert intent.price_min == 100


def test_price_between_and_range() -> None:
    intent = parse_query("pikachu between $10 and $50")
    assert (intent.price_min, intent.price_max) == (10, 50)
    assert intent.text == "pikachu"

    intent = parse_query("pikachu $5 to 20")
    assert (intent.price_min, intent.price_max) == (5, 20)


def test_sort_phrases() -> None:
    assert parse_query("most recent pikachu").sort == "newest"
    assert parse_query("latest releases pikachu").sort == "newest"
    assert parse_query("cheapest umbreon").sort == "price_asc"
    assert parse_query("most expensive charizard").sort == "price_desc"
    assert parse_query("oldest jungle cards").sort == "oldest"
    intent = parse_query("most recent pikachu")
    assert intent.text == "pikachu"
    assert "Newest first" in intent.chips


def test_game_aliases_and_diacritics() -> None:
    assert parse_query("mtg lightning bolt").game == "magic"
    assert parse_query("magic the gathering dragons").game == "magic"
    assert parse_query("yu-gi-oh blue-eyes").game == "yugioh"
    intent = parse_query("Pokémon cards")
    assert intent.game == "pokemon"
    assert intent.text == ""  # "cards" is noise once the game is extracted


def test_rarity_vocabulary() -> None:
    intent = parse_query("secret rare pikachu")
    assert intent.rarity_pattern == "secret"
    assert intent.text == "pikachu"
    assert parse_query("holo charizard").rarity_pattern == "holo|foil"
    assert parse_query("full art umbreon").rarity_pattern == "full art|illustration"


def test_set_phrase_forms() -> None:
    intent = parse_query("umbreon from evolving skies")
    assert intent.set_query == "evolving skies"
    assert intent.text == "umbreon"

    intent = parse_query("cards in the base set")
    assert intent.set_query == "base"
    assert intent.text == ""

    assert parse_query("evolving skies set").set_query == "evolving skies"


def test_year() -> None:
    intent = parse_query("charizard 1999")
    assert intent.year == 1999
    assert intent.text == "charizard"
    # "from 2021" is a YEAR, not a set phrase.
    intent = parse_query("cards from 2021")
    assert intent.year == 2021
    assert intent.set_query is None


# ── The compound "Google query" ──


def test_compound_query_parses_every_axis() -> None:
    intent = parse_query(
        "most recent secret rare pokemon under $50 from evolving skies"
    )
    assert intent.sort == "newest"
    assert intent.rarity_pattern == "secret"
    assert intent.game == "pokemon"
    assert intent.price_max == 50
    assert intent.set_query == "evolving skies"
    assert intent.text == ""
    assert not intent.plain and intent.has_signal
    assert {"Newest first", "Secret rare", "Pokémon", "Under $50"} <= set(intent.chips)


# ── Pool helpers ──


def test_filter_cards_applies_every_axis() -> None:
    cards = [
        _card("a", 10, "Secret Rare", "Evolving Skies", 2021),
        _card("b", 80, "Secret Rare", "Evolving Skies", 2021),
        _card("c", 12, "Common", "Evolving Skies", 2021),
        _card("d", 15, "Secret Rare", "Base", 1999),
        _card("e", None, "Secret Rare", "Evolving Skies", 2021),  # unpriced
    ]
    intent = QueryIntent(price_max=50, rarity_pattern="secret", year=2021)
    out = filter_cards(cards, intent, set_name="evolving skies")
    assert [c["id"] for c in out] == ["a"]


def test_sort_cards_newest_puts_undated_last() -> None:
    cards = [_card("a", year=1999), _card("b", year=2024), _card("c", year=None)]
    assert [c["id"] for c in sort_cards(cards, "newest")] == ["b", "a", "c"]
    assert [c["id"] for c in sort_cards(cards, "oldest")] == ["a", "b", "c"]


# ── Set resolution against the real (mocked) catalogs ──


@pytest.mark.asyncio
async def test_resolve_set_ranks_exact_then_newest(monkeypatch) -> None:
    async def fake_list_sets(tcg: str, sort: str = "catalog", limit=None):
        if tcg != "pokemon":
            return {"results": []}
        return {
            "results": [
                {
                    "id": "pokemontcg:base1",
                    "name": "Base",
                    "tcg": "pokemon",
                    "release_date": "1999/01/09",
                },
                {
                    "id": "pokemontcg:swsh7",
                    "name": "Evolving Skies",
                    "tcg": "pokemon",
                    "release_date": "2021/08/27",
                },
                {
                    "id": "pokemontcg:base2",
                    "name": "Base Set 2",
                    "tcg": "pokemon",
                    "release_date": "2000/02/24",
                },
            ]
        }

    monkeypatch.setattr(card_search_service, "list_sets", fake_list_sets)

    hit = await search_intel.resolve_set("evolving skies", "pokemon")
    assert hit is not None and hit["id"] == "pokemontcg:swsh7"

    # Exact beats prefix ("base" matches both; the exact name wins).
    hit = await search_intel.resolve_set("base", "pokemon")
    assert hit is not None and hit["name"] == "Base"

    # Prefix tie ("base set …") goes to the newest release.
    hit = await search_intel.resolve_set("base set", "pokemon")
    assert hit is not None and hit["name"] == "Base Set 2"

    assert await search_intel.resolve_set("no such set", "pokemon") is None
