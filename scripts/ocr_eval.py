#!/usr/bin/env python
"""OCR pipeline evaluation harness.

Downloads each fixture image, runs the full identification pipeline,
and reports top-1 / top-3 accuracy, mean confidence, latency
percentiles, and estimated cost. Designed to be run before/after every
non-trivial change to OCR parsing, scoring, or the chosen provider:

    OCR_PROVIDER=mock      python scripts/ocr_eval.py     # smoke test
    OCR_PROVIDER=google_vision python scripts/ocr_eval.py # real run

Outputs:

* Human-readable summary printed to stdout.
* CSV (``ocr_eval_<provider>.csv``) with one row per fixture for
  spreadsheet drill-down on misses.

The harness intentionally avoids the FastAPI router and goes straight
through :class:`CardIdentifier` so we measure the pipeline, not the
HTTP layer.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

# Allow running from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import get_sessionmaker  # noqa: E402
from app.services.identification.card_identifier import CardIdentifier  # noqa: E402
from app.services.ocr import get_provider  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ocr" / "fixtures.json"


@dataclass
class Result:
    fixture_id: str
    expected_name: str
    top1_name: str
    top1_correct: bool
    top3_correct: bool
    confidence: float
    latency_ms: int
    cost_usd: float
    parsed_title: str | None
    notes: str = ""


async def _download(url: str, client: httpx.AsyncClient) -> bytes:
    resp = await client.get(url, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _name_matches(expected: str, actual: str | None) -> bool:
    if not actual:
        return False
    return (
        expected.strip().lower() in actual.strip().lower()
        or actual.strip().lower() in expected.strip().lower()
    )


async def _run() -> list[Result]:
    fixtures = json.loads(FIXTURES.read_text())
    provider = get_provider()
    print(f"Provider: {provider.name}  Fixtures: {len(fixtures)}", flush=True)

    identifier = CardIdentifier()
    sessionmaker = get_sessionmaker()
    results: list[Result] = []

    async with httpx.AsyncClient(headers={"User-Agent": "loupe-ocr-eval/1.0"}) as http:
        for fx in fixtures:
            fx_id = fx["id"]
            print(f"→ {fx_id} …", end=" ", flush=True)
            try:
                image_bytes = await _download(fx["image_url"], http)
            except Exception as exc:
                print(f"download failed: {exc}")
                results.append(
                    Result(
                        fixture_id=fx_id,
                        expected_name=fx["expected_name"],
                        top1_name="",
                        top1_correct=False,
                        top3_correct=False,
                        confidence=0.0,
                        latency_ms=0,
                        cost_usd=0.0,
                        parsed_title=None,
                        notes=f"download_failed: {exc}",
                    )
                )
                continue

            async with sessionmaker() as db:
                t0 = time.perf_counter()
                outcome = await identifier.identify(
                    db,
                    image_bytes=image_bytes,
                    tcg_hint=fx.get("tcg"),
                )
                elapsed = int((time.perf_counter() - t0) * 1000)

            top3 = outcome.candidates[:3]
            top1 = top3[0] if top3 else None
            top1_correct = _name_matches(
                fx["expected_name"], top1.name if top1 else None
            )
            top3_correct = any(_name_matches(fx["expected_name"], c.name) for c in top3)
            results.append(
                Result(
                    fixture_id=fx_id,
                    expected_name=fx["expected_name"],
                    top1_name=top1.name if top1 else "",
                    top1_correct=top1_correct,
                    top3_correct=top3_correct,
                    confidence=outcome.accuracy_score,
                    latency_ms=elapsed,
                    cost_usd=outcome.cost_usd,
                    parsed_title=outcome.parsed.title,
                )
            )
            mark = "✓" if top1_correct else ("~" if top3_correct else "✗")
            print(
                f"{mark} top1={top1.name if top1 else '-'!r} conf={outcome.accuracy_score:.2f} {elapsed}ms"
            )

    return results


def _summarize(results: list[Result]) -> None:
    total = len(results)
    if total == 0:
        print("No results to summarize.")
        return
    top1 = sum(1 for r in results if r.top1_correct)
    top3 = sum(1 for r in results if r.top3_correct)
    latencies = [r.latency_ms for r in results if r.latency_ms]
    confidences = [r.confidence for r in results]
    cost_total = sum(r.cost_usd for r in results)
    print()
    print("=" * 60)
    print(f"Top-1 accuracy: {top1}/{total} = {top1 / total:.1%}")
    print(f"Top-3 accuracy: {top3}/{total} = {top3 / total:.1%}")
    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
        print(
            f"Latency p50: {p50}ms   p95: {p95}ms   mean: {statistics.mean(latencies):.0f}ms"
        )
    print(f"Mean confidence: {statistics.mean(confidences):.2f}")
    print(f"Estimated cost: ${cost_total:.4f}")
    print("=" * 60)


def _write_csv(results: list[Result], provider_name: str) -> Path:
    out = ROOT / f"ocr_eval_{provider_name}.csv"
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "fixture_id",
                "expected_name",
                "top1_name",
                "top1_correct",
                "top3_correct",
                "confidence",
                "latency_ms",
                "cost_usd",
                "parsed_title",
                "notes",
            ],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(r.__dict__)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        default=None,
        help="Override OCR_PROVIDER (mock | google_vision).",
    )
    args = parser.parse_args()
    if args.provider:
        import os

        os.environ["OCR_PROVIDER"] = args.provider
        get_settings.cache_clear()  # type: ignore[attr-defined]

    results = asyncio.run(_run())
    _summarize(results)
    csv_path = _write_csv(results, get_provider().name)
    print(f"CSV: {csv_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
