"""Measure the scanner's pHash robustness to real-world lighting + angles.

Fetches real card art, simulates how the same card looks under bad light or at
an angle (brightness/contrast/glare/rotation/perspective/blur/JPEG/low-res), and
reports how far the perceptual hash drifts against the production thresholds —
i.e. how often the fast path still identifies the card instantly.

    python -m scripts.scan_robustness            # default card set
    python -m scripts.scan_robustness 20         # more cards

Offline-safe sibling test: tests/identification/test_scan_robustness.py.
"""

from __future__ import annotations

import io
import sys
from collections import defaultdict

sys.path.insert(0, ".")

import httpx
from PIL import Image

from tests.identification.scan_robustness_fixtures import (
    AUGMENTATIONS,
    FAST_PATH_MAX,
    MATCH_MAX,
    hamming,
    normalized,
    phash_of,
)

# A spread of real card art (Pokémon TCG CDN — reliable + CORP-open).
_CARDS = [
    "https://images.pokemontcg.io/base1/58.png",
    "https://images.pokemontcg.io/base1/4.png",
    "https://images.pokemontcg.io/base1/2.png",
    "https://images.pokemontcg.io/base1/15.png",
    "https://images.pokemontcg.io/swsh1/1.png",
    "https://images.pokemontcg.io/swsh4/25.png",
    "https://images.pokemontcg.io/sm1/1.png",
    "https://images.pokemontcg.io/xy1/1.png",
    "https://images.pokemontcg.io/hgss1/1.png",
    "https://images.pokemontcg.io/dp1/1.png",
    "https://images.pokemontcg.io/ex1/1.png",
    "https://images.pokemontcg.io/neo1/1.png",
]


def _fetch(url: str) -> Image.Image | None:
    try:
        r = httpx.get(url, timeout=15.0, follow_redirects=True)
        if r.status_code >= 400:
            return None
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None


def main(n: int) -> None:
    urls = _CARDS[:n]
    print(f"fetching {len(urls)} cards…")
    cards = [(u, img) for u in urls if (img := _fetch(u)) is not None]
    print(f"  got {len(cards)}")
    if not cards:
        print("no cards fetched (network?) — try the offline test instead.")
        return

    # distance samples per augmentation
    dist: dict[str, list[int]] = defaultdict(list)
    cat_of: dict[str, str] = {}

    for _url, img in cards:
        base = normalized(img)
        clean = phash_of(base)
        for name, (cat, fn) in AUGMENTATIONS.items():
            cat_of[name] = cat
            try:
                aug = normalized(fn(img))
                dist[name].append(hamming(clean, phash_of(aug)))
            except Exception:
                dist[name].append(10_000)

    def pct(vals: list[int], thresh: int) -> float:
        return 100.0 * sum(1 for v in vals if v <= thresh) / len(vals)

    print("=" * 74)
    print(f"  SCAN ROBUSTNESS — {len(cards)} cards × {len(AUGMENTATIONS)} conditions")
    print(
        f"  fast-path ≤{FAST_PATH_MAX} bits (instant, no OCR) · match ≤{MATCH_MAX} bits"
    )
    print("=" * 74)
    order = sorted(AUGMENTATIONS, key=lambda k: (cat_of[k], k))
    cur = None
    cat_fast: dict[str, list[int]] = defaultdict(list)
    for name in order:
        cat = cat_of[name]
        if cat != cur:
            print(f"\n  [{cat.upper()}]")
            cur = cat
        vals = dist[name]
        mean = sum(vals) / len(vals)
        f, m = pct(vals, FAST_PATH_MAX), pct(vals, MATCH_MAX)
        cat_fast[cat].append(f)
        bar = "█" * int(m / 5)
        print(
            f"    {name:<18} mean={mean:5.1f}b  fast={f:5.1f}%  match={m:5.1f}%  {bar}"
        )

    print("\n" + "-" * 74)
    all_vals = [v for vals in dist.values() for v in vals]
    print(
        f"  OVERALL  fast-path {pct(all_vals, FAST_PATH_MAX):5.1f}%   "
        f"match {pct(all_vals, MATCH_MAX):5.1f}%   "
        f"(n={len(all_vals)} card×condition trials)"
    )
    for cat in sorted(cat_fast):
        cvals = [v for name in order if cat_of[name] == cat for v in dist[name]]
        print(
            f"    {cat:<10} fast-path {pct(cvals, FAST_PATH_MAX):5.1f}%   "
            f"match {pct(cvals, MATCH_MAX):5.1f}%"
        )
    print("=" * 74)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else len(_CARDS))
