"""Object storage helpers for user-facing reports (PDF statements).

In production (GCP) we persist statements to a real GCS bucket via the
native ``google-cloud-storage`` client using the Cloud Run service
account's Application Default Credentials — the SA already holds
``roles/storage.objectAdmin`` on ``settings.reports_bucket``. No HMAC
keys or S3 endpoint juggling required.

When ``google-cloud-storage`` isn't importable or a GCS client can't be
constructed (tests, local dev), we transparently fall back to the
existing S3-compatible client (``app.platform.s3``) — which itself
degrades to an in-memory stub. That keeps the test suite zero-config.

We never expose raw storage keys to clients — the router streams bytes
through :func:`download_report_bytes` and (when supported) signs
short-lived presigned GET URLs via :func:`generate_presigned_download_url`.
"""

from __future__ import annotations

import asyncio
import uuid

from app.config import get_settings
from app.platform.s3 import get_s3_client
from app.utils.logger import get_logger

try:  # Native GCS is the prod path; optional so tests stay light.
    from google.cloud import storage as _gcs  # type: ignore
except Exception:  # pragma: no cover - lib absent in minimal/test envs
    _gcs = None  # type: ignore[assignment]

_log = get_logger("services.reports.storage")

_gcs_client = None
_gcs_disabled = False


def _bucket() -> str:
    s = get_settings()
    # Allow operators to isolate reports in their own GCS bucket
    # (recommended in prod) while falling back to the existing scans
    # bucket so dev environments work zero-config.
    return getattr(s, "reports_bucket", None) or s.s3_bucket


def _get_gcs_client():
    """Lazily build a process-wide GCS client, or ``None`` if unavailable."""
    global _gcs_client, _gcs_disabled
    if _gcs is None or _gcs_disabled:
        return None
    if _gcs_client is None:
        try:
            _gcs_client = _gcs.Client()
        except Exception as exc:  # no ADC (local dev) → fall back quietly
            _log.warning("GCS client unavailable (%s); using S3/stub fallback", exc)
            _gcs_disabled = True
            return None
    return _gcs_client


def _use_gcs() -> bool:
    """True when a real GCS bucket + client are available (production)."""
    return bool(_bucket()) and _get_gcs_client() is not None


def build_storage_key(user_id: uuid.UUID, report_id: uuid.UUID) -> str:
    """Deterministic, user-scoped object key: ``reports/<user>/<report>.pdf``."""
    return f"reports/{user_id}/{report_id}.pdf"


async def upload_report_pdf(
    user_id: uuid.UUID, report_id: uuid.UUID, pdf_bytes: bytes
) -> str:
    """Persist a generated PDF and return the storage key it landed on."""
    if not pdf_bytes:
        raise ValueError("Refusing to upload an empty report PDF")
    key = build_storage_key(user_id, report_id)

    if _use_gcs():
        bucket_name = _bucket()

        def _put() -> None:
            client = _get_gcs_client()
            blob = client.bucket(bucket_name).blob(key)
            blob.upload_from_string(pdf_bytes, content_type="application/pdf")

        await asyncio.to_thread(_put)
        _log.info(
            "uploaded report %s to gs://%s/%s (%d bytes)",
            report_id,
            bucket_name,
            key,
            len(pdf_bytes),
        )
        return key

    client = get_s3_client()
    await client.put_object(
        bucket=_bucket(),
        key=key,
        body=pdf_bytes,
        content_type="application/pdf",
    )
    _log.info(
        "uploaded report %s for user %s (%d bytes)", report_id, user_id, len(pdf_bytes)
    )
    return key


async def download_report_bytes(storage_key: str) -> bytes | None:
    """Fetch the raw PDF bytes; ``None`` if the object has gone missing."""
    if _use_gcs():
        bucket_name = _bucket()

        def _get() -> bytes | None:
            client = _get_gcs_client()
            blob = client.bucket(bucket_name).blob(storage_key)
            if not blob.exists():
                return None
            return blob.download_as_bytes()

        return await asyncio.to_thread(_get)

    client = get_s3_client()
    return await client.get_object(bucket=_bucket(), key=storage_key)


async def generate_presigned_download_url(
    storage_key: str, *, expires_in: int = 900
) -> str | None:
    """Best-effort presigned GET URL.

    For GCS we deliberately return ``None`` and let the router stream the
    bytes through itself (the SA reads via ADC) — V4 signing would need a
    service-account key or an IAM SignBlob round-trip we don't want to
    require. The S3-compatible path still presigns when it can.
    """
    if _use_gcs():
        return None

    client = get_s3_client()
    generator = getattr(client, "generate_presigned_get_url", None)
    if generator is None:
        return None
    try:
        return await generator(
            bucket=_bucket(),
            key=storage_key,
            expires_in=expires_in,
        )
    except Exception as exc:  # pragma: no cover - presign is best-effort
        _log.warning("presign GET failed for %s: %s", storage_key, exc)
        return None


__all__ = [
    "build_storage_key",
    "download_report_bytes",
    "generate_presigned_download_url",
    "upload_report_pdf",
]
