"""Unit tests for OCR text parsing and confidence scoring.

Pure-Python — no DB, no provider — so they run sub-second and serve as
the regression suite when we tweak the title heuristics or rebalance
the score weights.
"""

from __future__ import annotations

from app.services.identification.confidence import (
    ScoreBreakdown,
    infer_tcg,
    score_candidate,
)
from app.services.identification.text_parser import parse_ocr_text

# ───────────────────────────────────────────────────────────── text parser


def test_parse_empty_returns_empty_parsed():
    parsed = parse_ocr_text("")
    assert parsed.title is None
    assert parsed.title_candidates == []
    assert parsed.hp is None
    assert parsed.tcg_hints == []


def test_parse_pokemon_extracts_title_hp_set_number_year():
    ocr = (
        "Charizard\n"
        "HP 120\n"
        "Fire Spin 100\n"
        "Discard 2 Energy attached to Charizard\n"
        "weakness Water x2\n"
        "BS  4/102\n"
        "Illus. Mitsuhiro Arita\n"
        "©1999 Wizards of the Coast\n"
    )
    parsed = parse_ocr_text(ocr)
    assert parsed.title == "Charizard"
    assert parsed.hp == 120
    assert parsed.card_number == "4/102"
    assert parsed.set_code == "BS"
    assert parsed.year == 1999
    assert "pokemon" in parsed.tcg_hints


def test_parse_magic_picks_up_mana_cost():
    ocr = "Lightning Bolt\n{R}\nInstant\nLightning Bolt deals 3 damage to any target.\n"
    parsed = parse_ocr_text(ocr)
    assert parsed.title == "Lightning Bolt"
    assert parsed.mana_cost == ["{R}"]
    assert "magic" in parsed.tcg_hints


def test_parse_yugioh_extracts_atk_def():
    ocr = "Dark Magician\nSpellcaster / Effect\nATK/2500 DEF/2100\nLOB-005\n"
    parsed = parse_ocr_text(ocr)
    assert parsed.title == "Dark Magician"
    assert parsed.atk_def == (2500, 2100)
    assert "yugioh" in parsed.tcg_hints


def test_parse_filters_legalese_from_title_candidates():
    ocr = "©2024 Pokemon\nIllus. Some Artist\nPikachu\nHP 60\n"
    parsed = parse_ocr_text(ocr)
    titles = [t for t, _ in parsed.title_candidates]
    assert "Pikachu" in titles
    assert not any("©" in t or "Illus" in t for t in titles)


# ─────────────────────────────────────────────────────────────── confidence


def test_infer_tcg_prefers_user_hint():
    parsed = parse_ocr_text("Dark Magician\nATK/2500 DEF/2100")
    assert infer_tcg(parsed, user_hint="pokemon") == "pokemon"


def test_infer_tcg_falls_back_to_parsed_hints():
    parsed = parse_ocr_text("Charizard\nHP 120\n")
    assert infer_tcg(parsed) == "pokemon"


def test_infer_tcg_defaults_to_all_when_no_signal():
    parsed = parse_ocr_text("Generic Text\n")
    assert infer_tcg(parsed) == "all"


def test_infer_tcg_biases_to_primary_on_fraction_number():
    # No decisive game signal (no HP / ATK-DEF / mana), but a NNN/NNN
    # collector number is soft evidence for the fraction-numbered games.
    # With the default primary TCG = "pokemon" this should resolve to
    # pokemon instead of fanning out to "all" (where a Yu-Gi-Oh result
    # could win on a poor read).
    parsed = parse_ocr_text("Some Card Name\n058/198\n")
    assert parsed.card_number == "058/198"
    assert infer_tcg(parsed) == "pokemon"


def test_infer_tcg_primary_bias_respects_decisive_signal():
    # A decisive ATK/DEF footer must still win over the primary bias.
    parsed = parse_ocr_text("Dark Magician\nATK/2500 DEF/2100\n")
    assert infer_tcg(parsed) == "yugioh"


def test_infer_tcg_primary_bias_disabled_when_set_to_all(monkeypatch):
    # Flipping the focus TCG to "all" turns the bias off — the scalable
    # escape hatch for a truly multi-game catalog.
    from app.services.identification import confidence as conf

    monkeypatch.setattr(
        conf.get_settings(), "identify_primary_tcg", "all", raising=False
    )
    parsed = parse_ocr_text("Some Card Name\n058/198\n")
    assert infer_tcg(parsed) == "all"


def test_score_candidate_rewards_exact_name_match():
    parsed = parse_ocr_text("Charizard\nHP 120\nBS 4/102\n")
    candidate = {
        "id": "pokemontcg:base1-4",
        "name": "Charizard",
        "set": {"code": "BS", "name": "Base Set"},
        "number": "4/102",
        "hp": "120",
    }
    breakdown = score_candidate(
        parsed=parsed,
        candidate=candidate,
        ocr_confidence=0.9,
        phash_hit=False,
    )
    assert isinstance(breakdown, ScoreBreakdown)
    assert breakdown.text_similarity > 0.9
    assert breakdown.field_match >= 0.5  # set + number + HP all matched
    assert breakdown.final > 0.7


def test_score_candidate_phash_hit_boosts_final():
    parsed = parse_ocr_text("Mystery\n")
    candidate = {"id": "pokemontcg:xy1-1", "name": "Venusaur EX"}
    no_phash = score_candidate(
        parsed=parsed, candidate=candidate, ocr_confidence=0.1, phash_hit=False
    )
    with_phash = score_candidate(
        parsed=parsed, candidate=candidate, ocr_confidence=0.1, phash_hit=True
    )
    assert with_phash.final > no_phash.final
    assert with_phash.phash_match == 1.0


def test_score_candidate_clamps_to_unit_interval():
    parsed = parse_ocr_text("X\n")
    candidate = {"id": "x", "name": "X"}
    breakdown = score_candidate(
        parsed=parsed,
        candidate=candidate,
        ocr_confidence=5.0,  # nonsense
        phash_hit=True,
        feedback_prior=99.0,
    )
    assert 0.0 <= breakdown.final <= 1.0
    assert 0.0 <= breakdown.ocr_quality <= 1.0
    assert 0.0 <= breakdown.feedback_prior <= 1.0
