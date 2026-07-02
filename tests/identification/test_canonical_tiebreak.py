"""Deterministic canonical tie-break for equal-confidence candidates.

Regression guard for the "scanner picks a random cheap reprint" bug: when
several printings share a name AND collector number (Charizard 4/102 in Base
Set, Base Set 2, and Legendary Collection) the weighted score ties them. The
pipeline must then return the ORIGINAL printing deterministically — earliest
year, then the smaller set, then a stable id — not whatever order the upstream
API happened to hand back.
"""

from __future__ import annotations

import dataclasses

from app.services.identification.card_identifier import CandidateOut, CardIdentifier
from app.services.identification.confidence import ScoreBreakdown


def _cand(cid: str, year: int | None, total: int | None) -> dict:
    return {
        "id": cid,
        "name": "Charizard",
        "number": "4",
        "year": year,
        "set": {"printed_total": total, "name": f"set-{cid}"},
    }


def _entry(cand: dict) -> tuple[CandidateOut, ScoreBreakdown, tuple[int, int, str]]:
    """A scored tuple with a fixed (tied) confidence, shaped like the pipeline's."""
    out = CandidateOut(
        card_id=None,
        upstream_id=cand["id"],
        name="Charizard",
        set_name=None,
        set_code=None,
        number="4",
        image_url=None,
        tcg="pokemon",
        confidence=0.75,
        source="text",
        breakdown={},
    )
    bd = ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.75)
    return (out, bd, CardIdentifier._canonical_rank_key(cand))


def test_canonical_key_prefers_earliest_year_then_smaller_set() -> None:
    base = _cand("pokemontcg:base1-4", 1999, 102)  # original
    b2 = _cand("pokemontcg:base2-4", 2000, 130)
    lc = _cand("pokemontcg:lc-4", 2002, 110)
    ordered = sorted([lc, b2, base], key=CardIdentifier._canonical_rank_key)
    assert ordered[0]["id"] == "pokemontcg:base1-4"


def test_rank_breaks_confidence_ties_by_canonical_key() -> None:
    # Deliberately feed the reprints first; the original must still surface.
    lc = _entry(_cand("pokemontcg:lc-4", 2002, 110))
    b2 = _entry(_cand("pokemontcg:base2-4", 2000, 130))
    base = _entry(_cand("pokemontcg:base1-4", 1999, 102))
    ranked = CardIdentifier._rank([lc, b2, base], 5)
    assert [c.upstream_id for c in ranked][:3] == [
        "pokemontcg:base1-4",
        "pokemontcg:base2-4",
        "pokemontcg:lc-4",
    ]


def test_missing_year_sorts_after_dated_printings() -> None:
    dated = _cand("pokemontcg:base1-4", 1999, 102)
    undated = {"id": "x:1", "name": "Charizard", "number": "4", "set": {}}
    ordered = sorted([undated, dated], key=CardIdentifier._canonical_rank_key)
    assert ordered[0]["id"] == "pokemontcg:base1-4"


def test_higher_confidence_still_wins_over_canonical() -> None:
    # A newer printing with a genuinely higher score (e.g. a pHash hit) must
    # NOT be demoted by the tie-break — the key only decides equal scores.
    s_out, s_bd, s_key = _entry(_cand("pokemontcg:lc-4", 2002, 110))
    strong = (dataclasses.replace(s_out, confidence=0.92), s_bd, s_key)
    weak_original = _entry(_cand("pokemontcg:base1-4", 1999, 102))
    ranked = CardIdentifier._rank([weak_original, strong], 5)
    assert ranked[0].upstream_id == "pokemontcg:lc-4"
