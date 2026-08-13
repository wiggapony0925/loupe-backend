"""The identification feedback loop must actually close.

WHAT WAS BROKEN. ``_to_candidate`` built every candidate with
``card_id=cand.get("card_id")`` — a key no producer emits. catalog_hash_index
returns ``"id"`` (catalog_hash_index.py:360), and so does every search path, so
``CandidateOut.card_id`` was None on every candidate ever produced.

That is not just a null column. The chain runs:

    candidate.card_id  ->  client sends it as the feedback answer
                       ->  identification_feedback.chosen_card_id
                       ->  _feedback_priors (filters chosen_card_id IS NOT NULL)
                       ->  feedback_priors.get(cand.get("id"))  -> ranking boost

With the first link None, every one of the 16 corrections real users made was
stored as NULL, `_feedback_priors` returned an empty map on every scan, and the
learning loop contributed nothing from launch until 2026-08-13.

These tests pin each link, and the last one pins the join between them — that
the key written at one end is the key looked up at the other. A test of the
column alone would have passed the whole time the loop was dead.
"""

from __future__ import annotations

from app.services.identification.card_identifier import CardIdentifier
from app.services.identification.confidence import ScoreBreakdown


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        text_similarity=0.9,
        ocr_quality=0.8,
        field_match=1.0,
        phash_match=1.0,
        feedback_prior=0.0,
        final=0.9,
    )


def _catalog_candidate() -> dict:
    """Exactly the shape catalog_hash_index.py:360 returns — note: no
    'card_id' key, which is the whole point."""
    return {
        "id": "scryfall:634f6f70-347e-436f-abf9-4a573472e2c8",
        "name": "Black Lotus",
        "tcg": "magic",
        "set_name": "Alpha",
        "number": "232",
        "image_url": "https://example.test/lotus.jpg",
    }


def test_a_catalog_candidate_carries_an_identifier():
    """The regression. This was None for every candidate ever built."""
    out = CardIdentifier._to_candidate(_catalog_candidate(), "phash", _breakdown())
    assert out.card_id is not None, (
        "card_id is None again — the clients send this as the feedback answer, "
        "so every correction will be stored as NULL and the learning loop dies"
    )
    assert out.card_id == "scryfall:634f6f70-347e-436f-abf9-4a573472e2c8"


def test_the_identifier_matches_the_key_the_prior_lookup_uses():
    """The join, and the reason a column-level test would not have caught this.

    `_feedback_priors` returns a map keyed by whatever was stored in
    `chosen_card_id`, and the caller looks it up with `cand.get("id")`
    (card_identifier.py:268 and :385). If the value written at one end is not
    the value read at the other, the map is populated and every lookup still
    misses.
    """
    cand = _catalog_candidate()
    out = CardIdentifier._to_candidate(cand, "phash", _breakdown())

    lookup_key = cand.get("id") or ""
    assert out.card_id == lookup_key, (
        f"the client will report {out.card_id!r} but the ranker looks up "
        f"{lookup_key!r} — the loop is open and priors will never hit"
    )


def test_a_real_local_card_id_still_wins_over_the_fallback():
    """The fallback must not clobber a producer that does resolve a card.

    Nothing emits one today — card_embedding_resolver is not yet wired into the
    live pipeline — but the `or` exists so that when one does, its value is
    kept rather than overwritten with the catalog id.
    """
    cand = _catalog_candidate() | {"card_id": "a-real-local-uuid"}
    out = CardIdentifier._to_candidate(cand, "embedding", _breakdown())
    assert out.card_id == "a-real-local-uuid"


def test_upstream_id_is_unchanged_by_the_fallback():
    """card_id gaining a value must not disturb the column beside it, which
    533 production rows already rely on."""
    out = CardIdentifier._to_candidate(_catalog_candidate(), "text", _breakdown())
    assert out.upstream_id == "scryfall:634f6f70-347e-436f-abf9-4a573472e2c8"


def test_a_candidate_with_no_identifier_at_all_stays_none():
    """Defensive: a producer that emits neither key must not yield an empty
    string, which would be stored and then matched against nothing."""
    out = CardIdentifier._to_candidate({"name": "Nameless"}, "text", _breakdown())
    assert out.card_id is None
