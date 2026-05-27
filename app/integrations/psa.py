"""PSA Public API provider — single-cert verification with Redis caching.

The free PSA Public API (https://www.psacard.com/publicapi) ONLY supports
cert verification by cert number — it does NOT expose bulk population data.
Free tier is **100 requests/day**, so every call is cached in Redis for 24h.

Population reports are intentionally not implemented (the API doesn't
support them); fan-out callers will get ``None`` and skip PSA cleanly.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from app.integrations.base import BaseProvider
from app.platform.redis_client import get_redis
from app.utils.logger import get_logger

logger = get_logger("integrations.psa")

_BASE = "https://api.psacard.com/publicapi"
_CACHE_TTL_SECONDS = 24 * 60 * 60
_CACHE_PREFIX = "psa:cert:"


class PsaProvider(BaseProvider):
    id = "psa"
    name = "PSA"

    def is_configured(self) -> bool:
        return bool(get_settings().psa_api_token)

    # Population deliberately NOT overridden — the public API doesn't support
    # it, so we keep the BaseProvider no-op so registry capabilities stay
    # honest.

    async def verify_cert(self, cert_no: str | int) -> dict[str, Any] | None:
        """Look up a PSA cert. Cached in Redis for 24h to stay under 100/day.

        Returns ``None`` when the cert is invalid, not found, or PSA is
        unreachable. Otherwise returns the ``PSACert`` payload PSA returns
        (subject, year, brand, category, grade, etc.).
        """
        if not self.is_configured():
            return None
        cert = "".join(c for c in str(cert_no) if c.isdigit())
        if not cert:
            return None

        cache_key = f"{_CACHE_PREFIX}{cert}"
        redis = None
        try:
            redis = await get_redis()
            cached = await redis.get(cache_key)
            if cached:
                payload = json.loads(cached)
                return None if payload == {"_miss": True} else payload
        except Exception as exc:
            logger.debug("psa cache get failed: %s", exc)
            redis = None

        token = get_settings().psa_api_token
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        url = f"{_BASE}/cert/GetByCertNumber/{cert}"
        try:
            resp = await self._call_with_retry("GET", url, headers=headers)
            if resp is None:
                return None
            if resp.status_code >= 500:
                return None  # don't cache infrastructure failures
            data = resp.json() or {}
        except Exception as exc:
            logger.warning("psa verify_cert failed: %s", exc)
            return None

        cert_info = _extract_cert(data)

        # Cache hits AND well-formed misses so we don't burn the 100/day quota.
        if redis is not None:
            try:
                to_cache = cert_info if cert_info is not None else {"_miss": True}
                await redis.setex(cache_key, _CACHE_TTL_SECONDS, json.dumps(to_cache))
            except Exception as exc:
                logger.debug("psa cache set failed: %s", exc)

        return cert_info


def _extract_cert(data: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce PSA's response to a clean dict, or None if the lookup failed."""
    if not isinstance(data, dict):
        return None
    msg = (data.get("ServerMessage") or "").lower()
    if "no data" in msg or "invalid" in msg:
        return None
    cert = data.get("PSACert") or data.get("Cert") or data.get("Item") or data
    if not isinstance(cert, dict):
        return None
    cert = {
        k: v for k, v in cert.items() if k not in {"IsValidRequest", "ServerMessage"}
    }
    return cert or None


__all__ = ["PsaProvider"]
