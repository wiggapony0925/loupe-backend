"""Async S3 client wrapper.

Uses aioboto3 against any S3-compatible endpoint (real S3, MinIO, etc).
Falls back to an in-process stub when aioboto3 isn't installed so that the
test suite + dev shell still work without cloud creds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.utils.logger import get_logger

logger = get_logger("clients.s3")

try:  # pragma: no cover - import-time guard
    import aioboto3
except Exception:  # pragma: no cover
    aioboto3 = None  # type: ignore[assignment]


class _StubS3:
    """Minimal in-memory stand-in for tests / offline dev."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self._lock = asyncio.Lock()

    async def put_object(
        self, bucket: str, key: str, body: bytes, content_type: str
    ) -> None:
        async with self._lock:
            self._objects[(bucket, key)] = body

    async def head_object(self, bucket: str, key: str) -> dict[str, Any] | None:
        async with self._lock:
            data = self._objects.get((bucket, key))
            if data is None:
                return None
            return {"ContentLength": len(data)}

    async def get_object(self, bucket: str, key: str) -> bytes | None:
        async with self._lock:
            return self._objects.get((bucket, key))

    async def generate_presigned_put_url(
        self,
        bucket: str,
        key: str,
        content_type: str,
        expires_in: int,
    ) -> str:
        # Stable, deterministic, obviously-fake URL for tests.
        return (
            f"https://stub-s3.local/{bucket}/{key}?ct={content_type}&exp={expires_in}"
        )


class _Aioboto3S3:
    """Real S3-compatible client backed by aioboto3."""

    def __init__(self) -> None:
        s = get_settings()
        self._session = aioboto3.Session(
            aws_access_key_id=s.s3_access_key_id,
            aws_secret_access_key=s.s3_secret_access_key,
            region_name=s.s3_region,
        )
        self._endpoint_url = s.s3_endpoint_url or None

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"service_name": "s3"}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        return kwargs

    async def put_object(
        self, bucket: str, key: str, body: bytes, content_type: str
    ) -> None:
        async with self._session.client(**self._client_kwargs()) as s3:
            await s3.put_object(
                Bucket=bucket, Key=key, Body=body, ContentType=content_type
            )

    async def head_object(self, bucket: str, key: str) -> dict[str, Any] | None:
        async with self._session.client(**self._client_kwargs()) as s3:
            try:
                return await s3.head_object(Bucket=bucket, Key=key)
            except Exception:
                return None

    async def get_object(self, bucket: str, key: str) -> bytes | None:
        async with self._session.client(**self._client_kwargs()) as s3:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
            except Exception:
                return None
            body = await resp["Body"].read()
            return bytes(body)

    async def generate_presigned_put_url(
        self,
        bucket: str,
        key: str,
        content_type: str,
        expires_in: int,
    ) -> str:
        async with self._session.client(**self._client_kwargs()) as s3:
            return await s3.generate_presigned_url(
                ClientMethod="put_object",
                Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires_in,
            )


_client: _Aioboto3S3 | _StubS3 | None = None


def get_s3_client() -> _Aioboto3S3 | _StubS3:
    """Return a process-wide S3 wrapper, real if creds available."""
    global _client
    if _client is not None:
        return _client
    s = get_settings()
    if aioboto3 is None or not (s.s3_access_key_id and s.s3_secret_access_key):
        logger.warning("S3 not configured or aioboto3 missing; using in-memory stub")
        _client = _StubS3()
    else:
        _client = _Aioboto3S3()
    return _client


def reset_s3_client() -> None:
    """Drop the cached S3 client (test hook)."""
    global _client
    _client = None


__all__ = ["get_s3_client", "reset_s3_client"]
