"""End-to-end scanner benchmark — latency + accuracy on camera-like frames.

The single source of truth for "is the scanner fast and right?". Reuses the
camera-degradation conditions from ``tests/identification/
scan_robustness_fixtures.py`` (rotation, perspective, JPEG, lighting) so the
frames look like real phone scans, then drives the REAL ``POST
/v1/cards/identify`` endpoint (prod or local) and scores:

* top-1 accuracy   — did the first candidate match the true card?
* latency p50/p95  — wall-clock per scan
* fast-path rate   — how many scans resolved via pHash (no OCR, no cost)

Usage:
    # against prod, save a labelled snapshot
    .venv/bin/python scripts/scanner_bench.py \
        --base https://loupe-api-….run.app --label before --out bench/before.json

    # compare two snapshots
    .venv/bin/python scripts/scanner_bench.py --compare bench/before.json bench/after.json
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.identification.scan_robustness_fixtures import (
    _jpeg,
    _perspective,
)

#: Real, popular cards across every game — the cards people actually scan.
#: (id, tcg, human name) — art is fetched from the public catalog.
BENCH_CARDS: list[tuple[str, str]] = [
    ("pokemontcg:base1-4", "pokemon"),  # Charizard, Base Set
    ("pokemontcg:base1-58", "pokemon"),  # Pikachu, Base Set
    ("pokemontcg:sv3-125", "pokemon"),  # Charizard ex
    ("pokemontcg:xy4-91", "pokemon"),  # AZ, Phantom Forces
    ("pokemontcg:swsh11-43", "pokemon"),
    ("pokemontcg:dp3-3", "pokemon"),  # Charizard, Secret Wonders
]

#: The camera-condition suite — what a phone actually produces. Clean art is
#: the control; the rest simulate hand-held reality.
CONDITIONS: list[tuple[str, Any]] = [
    ("clean", lambda im: im),
    ("rotate 3°", lambda im: im.rotate(3, expand=False, fillcolor=(24, 24, 28))),
    ("rotate 6°", lambda im: im.rotate(6, expand=False, fillcolor=(24, 24, 28))),
    ("perspective 12%", lambda im: _perspective(im, 0.12)),
    ("jpeg q40", lambda im: _jpeg(im, 40)),
    ("dim 60%", lambda im: Image.eval(im, lambda px: int(px * 0.6))),
    (
        "on-table",  # card pasted smaller onto a dark tabletop = loose framing
        lambda im: _paste_on_background(im, 0.82),
    ),
]


def _paste_on_background(card: Image.Image, scale: float) -> Image.Image:
    """Shrink the card and paste it on a dark 'desk' — loose framing."""
    w, h = card.size
    bg = Image.new("RGB", (w, h), (32, 30, 34))
    small = card.resize((int(w * scale), int(h * scale)))
    bg.paste(small, ((w - small.width) // 2, (h - small.height) // 2))
    return bg


async def _card_art(client: httpx.AsyncClient, base: str, cid: str) -> bytes | None:
    r = await client.get(f"{base}/v1/cards/{cid}")
    if r.status_code != 200:
        return None
    data = r.json().get("data") or {}
    images = data.get("images") or {}
    url = None
    for key in ("large", "normal", "small"):
        entry = images.get(key)
        if entry:
            url = entry.get("url") if isinstance(entry, dict) else entry
            break
    url = url or data.get("image_url")
    if not url:
        return None
    art = await client.get(url, headers={"User-Agent": "LoupeBench/1.0"})
    return art.content if art.status_code == 200 else None


async def run_bench(base: str, label: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for cid, tcg in BENCH_CARDS:
            raw = await _card_art(client, base, cid)
            if raw is None:
                print(f"  ! no art for {cid}, skipping")
                continue
            master = Image.open(io.BytesIO(raw)).convert("RGB")
            for cond_name, fn in CONDITIONS:
                frame = fn(master.copy())
                buf = io.BytesIO()
                frame.save(buf, format="JPEG", quality=78)
                payload = buf.getvalue()

                t0 = time.perf_counter()
                resp = await client.post(
                    f"{base}/v1/cards/identify",
                    files={"image": ("scan.jpg", payload, "image/jpeg")},
                    data={"tcg": tcg},
                )
                latency_ms = (time.perf_counter() - t0) * 1000
                cands = (
                    ((resp.json().get("data") or {}).get("candidates") or [])
                    if resp.status_code == 200
                    else []
                )
                top = cands[0] if cands else None
                correct = bool(top and top.get("upstream_id") == cid)
                source = (top or {}).get("source")
                rows.append(
                    {
                        "card": cid,
                        "condition": cond_name,
                        "latency_ms": round(latency_ms, 1),
                        "correct": correct,
                        "source": source,
                        "confidence": (top or {}).get("confidence"),
                    }
                )
                mark = "✓" if correct else "✗"
                print(
                    f"  {mark} {cid.split(':')[1]:>12} | {cond_name:<15} | "
                    f"{latency_ms:7.0f}ms | {source or '—'}"
                )

    lat = [r["latency_ms"] for r in rows]
    summary = {
        "label": label,
        "base": base,
        "n": len(rows),
        "top1_accuracy": round(sum(r["correct"] for r in rows) / max(1, len(rows)), 3),
        "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
        "latency_p95_ms": (
            round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else None
        ),
        "fast_path_rate": round(
            sum(1 for r in rows if r["source"] == "phash") / max(1, len(rows)), 3
        ),
        "by_condition": {},
        "rows": rows,
    }
    for cond_name, _ in CONDITIONS:
        sub = [r for r in rows if r["condition"] == cond_name]
        if sub:
            summary["by_condition"][cond_name] = {
                "accuracy": round(sum(r["correct"] for r in sub) / len(sub), 3),
                "p50_ms": round(statistics.median([r["latency_ms"] for r in sub]), 1),
                "phash_rate": round(
                    sum(1 for r in sub if r["source"] == "phash") / len(sub), 3
                ),
            }
    return summary


def compare(paths: list[str]) -> None:
    snaps = [json.loads(Path(p).read_text()) for p in paths]
    print(f"\n{'metric':<22}" + "".join(f"{s['label']:>14}" for s in snaps))
    for key in ("top1_accuracy", "latency_p50_ms", "latency_p95_ms", "fast_path_rate"):
        print(f"{key:<22}" + "".join(f"{s.get(key):>14}" for s in snaps))
    print("\nper-condition accuracy / p50:")
    conds = snaps[0]["by_condition"].keys()
    for c in conds:
        cells = []
        for s in snaps:
            e = s["by_condition"].get(c, {})
            cells.append(f"{e.get('accuracy', '—')} @ {e.get('p50_ms', '—')}ms")
        print(f"  {c:<18}" + "".join(f"{cell:>22}" for cell in cells))


async def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner end-to-end benchmark")
    ap.add_argument(
        "--base", default="https://loupe-api-714615078104.us-central1.run.app"
    )
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", nargs="+", default=None)
    args = ap.parse_args()

    if args.compare:
        compare(args.compare)
        return

    summary = await run_bench(args.base, args.label)
    print(
        f"\n[{summary['label']}] n={summary['n']} "
        f"top1={summary['top1_accuracy']:.0%} "
        f"p50={summary['latency_p50_ms']}ms p95={summary['latency_p95_ms']}ms "
        f"fast-path={summary['fast_path_rate']:.0%}"
    )
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2))
        print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
