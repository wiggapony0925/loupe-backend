"""Tests for smart catalog search query parsing."""

from __future__ import annotations

import pytest

from app.services.catalog.search_query import (
    bare_number,
    parse_search_query,
    pokemon_lucene_query,
    scryfall_query,
)


def test_bare_number_strips_leading_zeros():
    assert bare_number("058/102") == "58"
    assert bare_number("001/34") == "1"


def test_parse_collector_fraction():
    parsed = parse_search_query("charizard 4/102")
    assert parsed.number_bare == "4"
    assert parsed.number_raw == "4/102"
    assert parsed.name_tokens == ("charizard",)


def test_parse_standalone_fraction():
    parsed = parse_search_query("001/34")
    assert parsed.number_bare == "1"
    assert parsed.set_total == "34"
    assert parsed.name_tokens == ()


def test_parse_bare_digits():
    parsed = parse_search_query("58")
    assert parsed.number_bare == "58"
    assert parsed.name_text == ""


def test_pokemon_lucene_mixed():
    q = pokemon_lucene_query(parse_search_query("pikachu 58/102"))
    assert "name:pikachu*" in q
    assert "number:58" in q


def test_scryfall_mixed():
    q = scryfall_query(parse_search_query("lightning bolt 161"))
    assert "lightning" in q
    assert "cn:161" in q


def test_relevance_tolerates_charizard_typo():
    from app.services.catalog.card_search_service import relevance_score

    assert relevance_score("Charizard", "charzard") >= 0.75


@pytest.mark.parametrize(
    "typo",
    ["charzard", "charizard", "pikachu", "mewtwo"],
)
def test_typo_query_parses_as_name(typo: str):
    parsed = parse_search_query(typo)
    assert parsed.name_tokens == (typo.lower(),)
    assert parsed.has_number is False
