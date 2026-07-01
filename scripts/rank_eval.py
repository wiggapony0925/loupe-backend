"""Stress-test the card-identification RANKING engine over thousands of
synthetic-but-realistic payloads — offline, deterministic, in milliseconds.

This exercises the part of the pipeline that decides *which card wins*
(``score_candidate`` + sort + the synergy / number-conflict guards) without
any network, DB, or OCR provider. It reports top-1 accuracy on decidable
scenarios, top-2 recall on genuinely-ambiguous ones (reprint ties with no
readable set code — the scanner's "which one is it?" case), the number-conflict
invariant, and per-payload latency.

Usage::

    python -m scripts.rank_eval            # 5000 payloads
    python -m scripts.rank_eval 20000      # more

The committed test ``tests/identification/test_ranking_benchmark.py`` asserts
the same metrics with thresholds so a weight change can't silently regress
accuracy or speed.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict

# Allow ``python scripts/rank_eval.py`` from the backend root.
sys.path.insert(0, ".")

from tests.identification.ranking_fixtures import generate, rank


def main(n: int) -> None:
    scenarios = generate(n)

    # Time the pure ranking work only (generation excluded).
    per_call_us: list[float] = []
    top1_by_kind: dict[str, list[int]] = defaultdict(list)
    top2_by_kind: dict[str, list[int]] = defaultdict(list)
    conflict_violations = 0
    conflict_total = 0

    start = time.perf_counter()
    for sc in scenarios:
        t0 = time.perf_counter()
        ranked = rank(sc)
        per_call_us.append((time.perf_counter() - t0) * 1e6)

        ids = [c["id"] for c, _ in ranked]
        top1 = ids[0] == sc.truth_id
        top2 = sc.truth_id in ids[:2]
        top1_by_kind[sc.kind].append(1 if top1 else 0)
        top2_by_kind[sc.kind].append(1 if top2 else 0)

        if sc.has_conflict_distractor:
            conflict_total += 1
            # The winner must not be a same-name card with a conflicting number.
            if ids[0].startswith(("conflict:", "nlook:")):
                conflict_violations += 1
    wall = time.perf_counter() - start

    decidable = [sc for sc in scenarios if sc.decidable]
    ambiguous = [sc for sc in scenarios if not sc.decidable]

    dec_top1 = _rate(scenarios, lambda sc: sc.decidable, top1_by_kind)
    amb_top2 = _rate(scenarios, lambda sc: not sc.decidable, top2_by_kind)

    per_call_us.sort()
    p50 = per_call_us[len(per_call_us) // 2]
    p95 = per_call_us[int(len(per_call_us) * 0.95)]
    p99 = per_call_us[int(len(per_call_us) * 0.99)]

    print("=" * 66)
    print(f"  RANKING BENCHMARK — {n:,} payloads")
    print("=" * 66)
    print(f"  decidable scenarios : {len(decidable):,}")
    print(f"  ambiguous scenarios : {len(ambiguous):,} (reprint ties, no set code)")
    print("-" * 66)
    print(f"  TOP-1 accuracy (decidable) : {dec_top1 * 100:6.2f}%")
    print(f"  TOP-2 recall   (ambiguous) : {amb_top2 * 100:6.2f}%")
    print(
        f"  number-conflict invariant  : "
        f"{conflict_total - conflict_violations:,}/{conflict_total:,} held "
        f"({(1 - (conflict_violations / conflict_total if conflict_total else 0)) * 100:.2f}%)"
    )
    print("-" * 66)
    print("  per-kind TOP-1 / TOP-2:")
    for kind in sorted(top1_by_kind):
        t1 = top1_by_kind[kind]
        t2 = top2_by_kind[kind]
        print(
            f"    {kind:<22} n={len(t1):>6,}  "
            f"top1={sum(t1) / len(t1) * 100:6.2f}%  "
            f"top2={sum(t2) / len(t2) * 100:6.2f}%"
        )
    print("-" * 66)
    print(f"  latency/payload  p50={p50:6.1f}µs  p95={p95:6.1f}µs  p99={p99:6.1f}µs")
    print(
        f"  throughput       {n / wall:,.0f} payloads/s ({wall * 1000:.0f}ms for {n:,})"
    )
    print("=" * 66)


def _rate(scenarios, pred, table) -> float:
    """Fraction of ``pred`` scenarios whose truth landed in the top-N."""
    idx_by_kind: dict[str, int] = defaultdict(int)
    hits = 0
    total = 0
    for sc in scenarios:
        i = idx_by_kind[sc.kind]
        idx_by_kind[sc.kind] += 1
        if not pred(sc):
            continue
        total += 1
        hits += table[sc.kind][i]
    return hits / total if total else 0.0


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    main(count)
