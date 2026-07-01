"""Thousands-of-payloads benchmark for the identification RANKING engine.

Offline + deterministic (no network/DB/OCR): synthesizes thousands of realistic
adversarial identify payloads and asserts the ranker (``score_candidate`` + sort
+ synergy / number-conflict guards) both picks the right card and does so fast.
This is the CI guard behind the "always the right card, extremely fast" promise
— a weight change that regresses accuracy or speed fails here.

The runnable report lives at ``scripts/rank_eval.py`` (same fixtures).
"""

from __future__ import annotations

import time
from collections import defaultdict

from app.services.identification.confidence import score_candidate
from app.services.identification.text_parser import ParsedCard
from tests.identification.ranking_fixtures import Scenario, generate, rank

# Enough payloads to be a real stress test while staying well under a second.
_N = 3000


def _run(scenarios: list[Scenario]):
    """Rank every scenario; return (results, per-payload latency in µs)."""
    results: list[tuple[Scenario, list[str]]] = []
    latencies: list[float] = []
    for sc in scenarios:
        t0 = time.perf_counter()
        ranked = rank(sc)
        latencies.append((time.perf_counter() - t0) * 1e6)
        results.append((sc, [c["id"] for c, _ in ranked]))
    return results, latencies


def test_ranking_is_accurate_and_fast_over_thousands_of_payloads() -> None:
    scenarios = generate(_N)
    results, latencies = _run(scenarios)

    dec_hits = dec_total = 0
    amb_hits = amb_total = 0
    conflict_violations = 0
    per_kind_top1: dict[str, list[int]] = defaultdict(list)

    for sc, ids in results:
        top1 = ids[0] == sc.truth_id
        top2 = sc.truth_id in ids[:2]
        per_kind_top1[sc.kind].append(1 if top1 else 0)
        if sc.decidable:
            dec_total += 1
            dec_hits += top1
        else:
            amb_total += 1
            amb_hits += top2
        if sc.has_conflict_distractor and ids[0].startswith(("conflict:", "nlook:")):
            conflict_violations += 1

    dec_acc = dec_hits / dec_total
    amb_recall = amb_hits / amb_total

    # Decidable payloads: when the signal uniquely identifies the card, the
    # ranker must pick it essentially every time.
    assert dec_acc >= 0.99, (
        f"decidable top-1 accuracy regressed: {dec_acc:.4f} "
        f"(per-kind: {{k: sum(v)/len(v) for k,v in per_kind_top1.items()}})"
    )
    # Genuinely ambiguous ties (reprint, no readable set code): the truth must
    # be in the top 2 so the UI can ask the user to confirm the exact printing.
    assert amb_recall >= 0.99, f"ambiguous top-2 recall regressed: {amb_recall:.4f}"
    # The core "never lock the wrong card" invariant: a same-name look-alike
    # whose collector number conflicts with a clean read must never win.
    assert conflict_violations == 0, (
        f"{conflict_violations} number-conflict look-alikes wrongly ranked #1"
    )

    # Speed: pure-Python scoring is microseconds. A hugely generous ceiling
    # (real p95 ≈ 0.15ms) that still fails if someone makes ranking O(n²) or
    # adds a network call — without flaking on a loaded CI runner.
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99)]
    mean = sum(latencies) / len(latencies)
    assert p99 < 5000, f"p99 ranking latency too slow: {p99:.0f}µs"
    assert mean < 2000, f"mean ranking latency too slow: {mean:.0f}µs"


def test_number_conflict_never_beats_a_clean_number_match() -> None:
    """A same-name card with a *different* number loses to the true card even
    when its name is an identical (perfect) match — the number is the tiebreak."""
    parsed = ParsedCard(
        title="Pikachu",
        title_candidates=[("Pikachu", 0.95)],
        set_code=None,
        card_number="58/102",
    )
    truth = {"id": "t", "name": "Pikachu", "number": "58/102", "set": {"code": "BS"}}
    lookalike = {
        "id": "x",
        "name": "Pikachu",
        "number": "285/264",
        "set": {"code": "SV1"},
    }
    st = score_candidate(
        parsed=parsed, candidate=truth, ocr_confidence=0.8, phash_hit=False
    )
    sx = score_candidate(
        parsed=parsed, candidate=lookalike, ocr_confidence=0.8, phash_hit=False
    )
    assert st.final > sx.final


def test_set_plus_number_synergy_wins_when_the_name_reads_poorly() -> None:
    """Vintage/foil case: the name OCRs badly, but set code + number agree —
    the correctly-pinned printing still outranks a clean name-only match."""
    parsed = ParsedCard(
        title="Charlzurd",  # mangled "Charizard"
        title_candidates=[("Charlzurd", 0.6)],
        set_code="BS",
        card_number="4/102",
    )
    pinned = {
        "id": "pin",
        "name": "Charizard",
        "number": "4/102",
        "set": {"code": "BS"},
    }
    name_only = {
        "id": "nm",
        "name": "Charlzurd",
        "number": "9/99",
        "set": {"code": "XY1"},
    }
    s_pin = score_candidate(
        parsed=parsed, candidate=pinned, ocr_confidence=0.7, phash_hit=False
    )
    s_nm = score_candidate(
        parsed=parsed, candidate=name_only, ocr_confidence=0.7, phash_hit=False
    )
    assert s_pin.final > s_nm.final


def test_reprint_with_set_code_resolves_to_the_right_printing() -> None:
    """Two identical name+number reprints; only the parsed set code differs —
    the ranker must pick the printing whose set code matches."""
    parsed = ParsedCard(
        title="Dark Magician",
        title_candidates=[("Dark Magician", 0.95)],
        set_code="LOB",
        card_number="46/70",
    )
    a = {
        "id": "lob",
        "name": "Dark Magician",
        "number": "46/70",
        "set": {"code": "LOB"},
    }
    b = {
        "id": "sdy",
        "name": "Dark Magician",
        "number": "46/70",
        "set": {"code": "SDY"},
    }
    sa = score_candidate(
        parsed=parsed, candidate=a, ocr_confidence=0.85, phash_hit=False
    )
    sb = score_candidate(
        parsed=parsed, candidate=b, ocr_confidence=0.85, phash_hit=False
    )
    assert sa.final > sb.final
