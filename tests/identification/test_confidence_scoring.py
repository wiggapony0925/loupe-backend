"""Confidence-scoring calibration tests.

These pin the behaviour that makes the live scanner actually lock onto a
card: a parsed (title + collector number) that both match a candidate is
a near-certain identification and must score well clear of name-only
look-alikes (promos, reprints) so the client crosses its lock threshold.
Regression guard for the Base Set Pikachu #58 bug, where the exact card
scored 0.55 — below the 0.7 lock — and the scanner felt like it "never
recognised" obvious cards.
"""

from __future__ import annotations

from app.services.identification.confidence import score_candidate
from app.services.identification.text_parser import ParsedCard

# Mirrors the real Google Vision parse of a Base Set Pikachu #58 photo.
_PARSED = ParsedCard(
    title="Pikachu",
    title_candidates=[("Pikachu", 1.0)],
    set_code=None,
    card_number="58/102",
    year=1999,
    hp=40,
)

_EXACT = {
    "id": "pokemontcg:base1-58",
    "name": "Pikachu",
    "number": "58",
    "set": {"code": "base1"},
    "hp": "40",
    "year": 1999,
}
_PROMO = {
    "id": "pokemontcg:basep-1",
    "name": "Pikachu",
    "number": "1",
    "set": {"code": "basep"},
    "hp": "60",
    "year": 1999,
}


def test_title_plus_number_match_crosses_lock_threshold() -> None:
    score = score_candidate(
        parsed=_PARSED, candidate=_EXACT, ocr_confidence=0.93, phash_hit=False
    )
    # Must clear the client lock threshold (0.7) so the scanner commits.
    assert score.final >= 0.7
    assert score.field_match >= 0.3  # number bonus present


def test_exact_card_outranks_name_only_lookalike() -> None:
    exact = score_candidate(
        parsed=_PARSED, candidate=_EXACT, ocr_confidence=0.93, phash_hit=False
    )
    promo = score_candidate(
        parsed=_PARSED, candidate=_PROMO, ocr_confidence=0.93, phash_hit=False
    )
    assert exact.final > promo.final
    # The look-alike (right name, wrong number) must NOT get the synergy
    # lift and should stay in carousel territory below the lock.
    assert promo.final < 0.7


def test_no_synergy_without_number_match() -> None:
    no_number = ParsedCard(
        title="Pikachu",
        title_candidates=[("Pikachu", 1.0)],
        card_number=None,
        hp=40,
    )
    score = score_candidate(
        parsed=no_number, candidate=_EXACT, ocr_confidence=0.93, phash_hit=False
    )
    # Without a parsed number there's no synergy bonus, so a name-only
    # match stays below lock.
    assert score.final < 0.7


def test_set_code_plus_number_synergy_lifts_poor_name_match() -> None:
    # Vintage/foil case: Vision mangled the title, but the set symbol +
    # collector number read clean. set_code + number uniquely pin a printing,
    # so the frame that reads BOTH must score materially higher than the same
    # frame that couldn't read the set code — even with a weak name match.
    common = {
        "title": "zzqx",  # deliberately unlike "Pikachu" → low text similarity
        "title_candidates": [("zzqx", 0.4)],
        "card_number": "58/102",
        "hp": 40,
    }
    with_set = ParsedCard(set_code="base1", **common)
    without_set = ParsedCard(set_code=None, **common)
    s_with = score_candidate(
        parsed=with_set, candidate=_EXACT, ocr_confidence=0.6, phash_hit=False
    )
    s_without = score_candidate(
        parsed=without_set, candidate=_EXACT, ocr_confidence=0.6, phash_hit=False
    )
    # The gap must exceed the bare set-code field bonus alone (~0.175),
    # proving the set+number synergy actually fired.
    assert s_with.final > s_without.final + 0.3


# ── Wrong-match guard ──────────────────────────────────────────────────
# Regression for the live "Pikachu read as Energizer #285" bug: an attack
# name ("Energize") fuzzy-matches a different card ("Energizer") whose
# printed number contradicts the number read off the card. A number
# conflict must sink the look-alike well below both the real card and the
# lock threshold, no matter how close the names read.

# Mirrors a Pikachu #049 frame where OCR mis-promoted the attack name.
_PIKACHU_FRAME = ParsedCard(
    title="Pikachu",
    title_candidates=[("Pikachu", 1.0), ("Energize", 0.82)],
    set_code=None,
    card_number="49/203",
    year=2021,
    hp=60,
)
_REAL_PIKACHU = {
    "id": "pokemontcg:cel25-49",
    "name": "Pikachu",
    "number": "49",
    "set": {"code": "cel25"},
    "hp": "60",
    "year": 2021,
}
# The wrong card the scanner locked onto: right-ish name, wrong number.
_ENERGIZER_LOOKALIKE = {
    "id": "pokemontcg:sv4-285",
    "name": "Energizer",
    "number": "285",
    "set": {"code": "sv4"},
    "hp": None,
    "year": 2023,
}


def test_number_conflict_sinks_name_lookalike() -> None:
    real = score_candidate(
        parsed=_PIKACHU_FRAME,
        candidate=_REAL_PIKACHU,
        ocr_confidence=0.9,
        phash_hit=False,
    )
    lookalike = score_candidate(
        parsed=_PIKACHU_FRAME,
        candidate=_ENERGIZER_LOOKALIKE,
        ocr_confidence=0.9,
        phash_hit=False,
    )
    # The real card must win decisively and the look-alike must stay far
    # below the lock threshold so the scanner never commits to it.
    assert real.final > lookalike.final
    assert lookalike.final < 0.5
    assert real.final >= 0.7


def test_missing_candidate_number_is_not_a_conflict() -> None:
    # A candidate with no number is "unknown", not a conflict — it must
    # not be penalised just because the catalog omitted its number.
    no_number_card = {"id": "x", "name": "Pikachu", "set": {"code": "cel25"}}
    score = score_candidate(
        parsed=_PIKACHU_FRAME,
        candidate=no_number_card,
        ocr_confidence=0.9,
        phash_hit=False,
    )
    # Name matches, no number to conflict → stays a respectable score.
    assert score.final >= 0.44
