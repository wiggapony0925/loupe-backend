"""Invariants for the image-embedding scaffold (embeddings + pgvector upgrade).

Verifies the properties the embedding *does* deliver — rotation- and
lighting-robustness (the pHash gap) — plus determinism and L2-normalisation, so
the interface is a sound drop-in point for a CNN encoder later. It deliberately
does NOT assert cross-card discrimination: the classical extractor is not
discriminative enough (see the module docstring), which is why it stays out of
the live resolver until the feature extractor is a learned model.
"""

from __future__ import annotations

import io

from PIL import Image, ImageEnhance

from app.services.identification.card_embedding_service import (
    EMBED_DIM,
    cosine,
    embed_image_bytes,
)
from tests.identification.scan_robustness_fixtures import synthetic_card


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_embedding_is_deterministic_and_normalized() -> None:
    vec = embed_image_bytes(_png(synthetic_card(1)))
    assert vec is not None
    assert len(vec) == EMBED_DIM
    # L2-normalised → self-cosine ≈ 1.
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-4
    assert cosine(vec, vec) > 0.999
    # Deterministic.
    again = embed_image_bytes(_png(synthetic_card(1)))
    assert vec == again


def test_embedding_survives_rotation() -> None:
    """The whole point: a rotated card stays close in cosine, where the pHash
    would drift out of range. This is the angle gap the embedding closes."""
    for seed in range(6):
        img = synthetic_card(seed)
        base = embed_image_bytes(_png(img))
        assert base is not None
        for deg in (8, 18, 25):
            rot = img.rotate(deg, expand=False, fillcolor=(20, 20, 20))
            c = cosine(base, embed_image_bytes(_png(rot)) or [])
            assert c >= 0.80, f"seed {seed} @ {deg}°: cosine {c:.3f} too low"


def test_embedding_survives_lighting() -> None:
    for seed in range(6):
        img = synthetic_card(seed)
        base = embed_image_bytes(_png(img))
        assert base is not None
        for enhance in (0.5, 1.6):
            lit = ImageEnhance.Brightness(img).enhance(enhance)
            c = cosine(base, embed_image_bytes(_png(lit)) or [])
            assert c >= 0.72, f"seed {seed} @ ×{enhance}: cosine {c:.3f} too low"


def test_cosine_handles_edge_cases() -> None:
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0]) == 0.0  # length mismatch
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9  # orthogonal
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
