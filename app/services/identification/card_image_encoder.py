"""Learned CNN image encoder (ONNX Runtime) for card recognition.

The production drop-in the classical ``card_embedding_service`` scaffold was
built for: a CLIP-style image encoder → a fixed-length L2-normalised vector,
robust to distance / blur / glare where pHash + colour-histograms are brittle.
The catalog is embedded once (``scripts/backfill_embeddings.py``) into the
``catalog_card_embeddings`` pgvector table;
:func:`card_resolver_service.resolve_by_embedding` ranks it by nearest
neighbour at scan time.

The model is a CLIP image encoder exported to ONNX (see the PR runbook), its
path in ``Settings.card_embed_model_path``. When the model / onnxruntime is
unavailable this returns ``None`` and the matcher no-ops — identify falls back
to pHash + OCR — so it is safe to ship dark and flip on once back-filled.
"""

from __future__ import annotations

import io
import threading

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("services.identification.encoder")

# CLIP-style preprocessing (override if you swap encoders).
_INPUT_SIZE = 224
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)

_session = None  # onnxruntime.InferenceSession, lazily created
_session_lock = threading.Lock()
_load_failed = False


def encoder_available() -> bool:
    """True when a model is loaded (or loadable) — cheap gate for callers."""
    return _get_session() is not None


def _get_session():
    global _session, _load_failed
    if _session is not None or _load_failed:
        return _session
    with _session_lock:
        if _session is not None or _load_failed:
            return _session
        path = get_settings().card_embed_model_path
        if not path:
            _load_failed = True
            return None
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]

            _session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            logger.info("encoder: loaded ONNX model from %s", path)
        except Exception as exc:  # pragma: no cover - infra-dependent
            logger.warning("encoder: could not load model (%s); matcher disabled", exc)
            _load_failed = True
        return _session


def embed_image_bytes(data: bytes) -> list[float] | None:
    """L2-normalised CNN embedding for a card image, or ``None`` when the
    encoder is unavailable / the bytes don't decode."""
    session = _get_session()
    if session is None:
        return None
    try:
        import numpy as np
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover
        return None

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (_INPUT_SIZE * 2, _INPUT_SIZE * 2))
            im.load()
            rgb = ImageOps.exif_transpose(im) or im
            rgb = rgb.convert("RGB").resize(
                (_INPUT_SIZE, _INPUT_SIZE), Image.Resampling.BILINEAR
            )
        arr = np.asarray(rgb, dtype=np.float32) / 255.0
        mean = np.array(_MEAN, dtype=np.float32)
        std = np.array(_STD, dtype=np.float32)
        norm_arr = ((arr - mean) / std).astype(np.float32)
        chw = np.transpose(norm_arr, (2, 0, 1))[None, ...].astype(np.float32)  # NCHW
    except Exception as exc:
        logger.info("encoder: preprocess failed (%s)", exc)
        return None

    try:
        input_name = session.get_inputs()[0].name
        out = session.run(None, {input_name: chw})[0]
        vec = np.asarray(out, dtype=np.float32).reshape(-1)
    except Exception as exc:  # pragma: no cover - infra-dependent
        logger.warning("encoder: inference failed (%s)", exc)
        return None

    norm = float(np.linalg.norm(vec))
    if norm < 1e-8:
        return None
    return (vec / norm).tolist()


__all__ = ["embed_image_bytes", "encoder_available"]
