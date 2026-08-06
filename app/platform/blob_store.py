"""Unified blob storage — THE way backend code reads/writes object bytes.

Backend deployments run on Google Cloud Storage (Cloud Run's service
account already holds ``roles/storage.objectAdmin``), but local dev and CI
have no cloud at all. Feature code must not care: it calls
:func:`put_object` / :func:`get_object` with a bucket + key and this module
picks the backend:

1. **GCS native** (production) — explicit opt-in via the ``GCS_BUCKET``
   setting, authenticated by Application Default Credentials. The env var
   is the switch on purpose: a dev machine with stray ADC must never write
   to real buckets just because the library is importable.
2. **S3-compatible** (``app.platform.s3``) — MinIO in local dev; degrades
   to an in-process stub when creds are empty (the test suite's path).

History: avatars originally called the S3 client directly, which in
production meant aioboto3 + default MinIO creds against REAL AWS →
``InvalidAccessKeyId`` 500s on every profile-picture upload. Storage
backend selection lives HERE now so a feature can't repeat that.
"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.platform.s3 import get_s3_client
from app.utils.logger import get_logger

try:  # Native GCS is the prod path; optional so tests stay light.
    from google.cloud import storage as _gcs  # type: ignore
except Exception:  # pragma: no cover - lib absent in minimal/test envs
    _gcs = None  # type: ignore[assignment]

logger = get_logger("platform.blob_store")

_gcs_client = None
_gcs_disabled = False


def _get_gcs_client():
    """Lazily build a process-wide GCS client, or ``None`` if unavailable."""
    global _gcs_client, _gcs_disabled
    if _gcs is None or _gcs_disabled:
        return None
    if _gcs_client is None:
        try:
            _gcs_client = _gcs.Client()
        except Exception as exc:  # no ADC → fall back quietly
            logger.warning("GCS client unavailable (%s); using S3/stub fallback", exc)
            _gcs_disabled = True
            return None
    return _gcs_client


def gcs_mode() -> bool:
    """True when this deployment explicitly runs on GCS (``GCS_BUCKET`` set)."""
    return bool(get_settings().gcs_bucket) and _get_gcs_client() is not None


async def put_object(bucket: str, key: str, body: bytes, content_type: str) -> None:
    """Write ``body`` at ``bucket/key`` (overwrites in place)."""
    if gcs_mode():

        def _put() -> None:
            client = _get_gcs_client()
            blob = client.bucket(bucket).blob(key)
            blob.upload_from_string(body, content_type=content_type)

        await asyncio.to_thread(_put)
        return
    await get_s3_client().put_object(
        bucket=bucket, key=key, body=body, content_type=content_type
    )


async def get_object(bucket: str, key: str) -> bytes | None:
    """Raw bytes at ``bucket/key``; ``None`` when the object doesn't exist."""
    if gcs_mode():

        def _get() -> bytes | None:
            client = _get_gcs_client()
            blob = client.bucket(bucket).blob(key)
            if not blob.exists():
                return None
            return bytes(blob.download_as_bytes())

        return await asyncio.to_thread(_get)
    return await get_s3_client().get_object(bucket=bucket, key=key)


def reset_blob_store() -> None:
    """Drop the cached GCS client / disabled flag (test hook)."""
    global _gcs_client, _gcs_disabled
    _gcs_client = None
    _gcs_disabled = False


__all__ = ["gcs_mode", "get_object", "put_object", "reset_blob_store"]
