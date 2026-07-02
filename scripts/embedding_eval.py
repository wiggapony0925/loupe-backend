"""Embedding vs pHash — rotation robustness AND cross-card discrimination.

Shows, on real card art, why the classical colour embedding is the right
*interface* but the wrong *extractor*: it survives rotation (where pHash fails)
but can't reliably tell same-card-rotated from a different card. The gap between
the two is the discrimination a CNN encoder would supply.

    python -m scripts.embedding_eval
"""

from __future__ import annotations

import io
import sys

sys.path.insert(0, ".")

import httpx
from PIL import Image

from app.services.catalog.card_fingerprint_service import (
    fingerprint_from_image_bytes,
)
from app.services.identification.card_embedding_service import (
    cosine,
    embed_image_bytes,
)

_CARDS = {
    "Charizard": "base1/4",
    "Blastoise": "base1/2",
    "Pikachu": "base1/58",
    "Venusaur": "base1/15",
    "Mewtwo": "base1/10",
}


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _hamming(a: str, b: str) -> int:
    return (
        bin(int(a, 16) ^ int(b, 16)).count("1") if a and b and len(a) == len(b) else 999
    )


def main() -> None:
    imgs: dict[str, Image.Image] = {}
    for name, path in _CARDS.items():
        try:
            r = httpx.get(f"https://images.pokemontcg.io/{path}.png", timeout=15)
            imgs[name] = Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
    if len(imgs) < 2:
        print("could not fetch cards (network?)")
        return

    emb = {n: embed_image_bytes(_png(im)) or [] for n, im in imgs.items()}
    ph = {n: fingerprint_from_image_bytes(_png(im)).phash for n, im in imgs.items()}  # type: ignore[union-attr]

    print("=" * 66)
    print("  SAME card rotated — pHash drifts out of range, embedding holds")
    print("=" * 66)
    worst_same = 1.0
    for n, im in imgs.items():
        parts = []
        for deg in (8, 18, 25):
            rot = im.rotate(deg, expand=False, fillcolor=(20, 20, 20))
            d = _hamming(ph[n], fingerprint_from_image_bytes(_png(rot)).phash)  # type: ignore[union-attr]
            c = cosine(emb[n], embed_image_bytes(_png(rot)) or [])
            worst_same = min(worst_same, c)
            parts.append(f"{deg}° pHash={d:>3}b cos={c:.3f}")
        print(f"  {n:<10} " + "  ".join(parts))

    print("\n" + "=" * 66)
    print("  DIFFERENT cards — embedding cosine (should be well below same-card)")
    print("=" * 66)
    names = list(imgs)
    best_diff = 0.0
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            c = cosine(emb[names[i]], emb[names[j]])
            best_diff = max(best_diff, c)
            print(f"  {names[i]:<10} vs {names[j]:<10} cos={c:.3f}")

    print("\n" + "-" * 66)
    print(f"  worst same-card-rotated cosine : {worst_same:.3f}")
    print(f"  best  different-card cosine     : {best_diff:.3f}")
    verdict = (
        "SEPARABLE — embedding could ID under rotation"
        if worst_same > best_diff
        else "OVERLAP — colour alone can't discriminate; needs a CNN encoder"
    )
    print(f"  verdict: {verdict}")
    print("=" * 66)


if __name__ == "__main__":
    main()
