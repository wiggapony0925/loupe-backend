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
