"""Object storage helpers for user-facing reports (PDF statements).

Reads/writes ride :mod:`app.platform.blob_store` — GCS native in
production (ADC via the Cloud Run service account), S3-compatible/stub
elsewhere — so this module only decides bucket + key, never backend.

We never expose raw storage keys to clients — the router streams bytes
through :func:`download_report_bytes` and (when supported) signs
short-lived presigned GET URLs via :func:`generate_presigned_download_url`.
"""

from __future__ import annotations

import uuid

from app.config import get_settings
from app.platform import blob_store
from app.platform.s3 import get_s3_client
from app.utils.logger import get_logger

_log = get_logger("services.reports.storage")


def _bucket() -> str:
    s = get_settings()
    # Allow operators to isolate reports in their own GCS bucket
    # (recommended in prod) while falling back to the existing scans
    # bucket so dev environments work zero-config.
    return getattr(s, "reports_bucket", None) or s.s3_bucket


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

    await blob_store.put_object(
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
    return await blob_store.get_object(bucket=_bucket(), key=storage_key)


async def generate_presigned_download_url(
    storage_key: str, *, expires_in: int = 900
) -> str | None:
    """Best-effort presigned GET URL.

    For GCS we deliberately return ``None`` and let the router stream the
    bytes through itself (the SA reads via ADC) — V4 signing would need a
    service-account key or an IAM SignBlob round-trip we don't want to
    require. The S3-compatible path still presigns when it can.
    """
    if blob_store.gcs_mode():
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
