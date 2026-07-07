"""FX rates — ONE source of truth for display-currency conversion.

Both clients (loupe-web and loupe-frontend) used to ship their own
hard-coded `ratePerUsd` tables, which drifted from reality (and from each
other) the day they were written. This service centralizes the math:

  • Fiat rates come from open.er-api.com (keyless, daily-updated).
  • Crypto rates come from CoinGecko's keyless simple-price endpoint.
  • The merged table is cached in the durable L2 (`kv_cache`) for 12 h so
    a whole fleet serves rates with zero upstream calls per request.
  • Any upstream failure degrades to the static snapshot below — the
    endpoint NEVER 5xxs and always returns a complete table.

Semantics: ``rates[code]`` = units of *code* per **1 USD** (matches the
clients' historical ``ratePerUsd``). Base is always USD because every
price Loupe stores is USD.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.platform.cache_l2 import kv_get, kv_set
from app.utils.logger import get_logger

logger = get_logger("services.fx")

_CACHE_KEY = "fx:rates:v1"
_CACHE_TTL_SECONDS = 12 * 60 * 60
_FETCH_TIMEOUT_S = 8.0

FIAT_CODES = [
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY",
    "HKD", "SGD", "KRW", "INR", "MXN", "BRL", "AED",
]

#: CoinGecko id → display code.
CRYPTO_IDS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "usd-coin": "USDC",
}

#: Last-resort snapshot (kept in sync with the clients' historical tables).
#: Only served when both the cache AND the live fetch are unavailable.
STATIC_RATES: dict[str, float] = {
    "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "JPY": 156.4, "CAD": 1.37,
    "AUD": 1.51, "CHF": 0.91, "CNY": 7.24, "HKD": 7.81, "SGD": 1.35,
    "KRW": 1378.0, "INR": 83.4, "MXN": 17.1, "BRL": 5.12, "AED": 3.67,
    "BTC": 1 / 67_400, "ETH": 1 / 3_120, "SOL": 1 / 152, "USDC": 1.0,
}


async def _fetch_fiat(client: httpx.AsyncClient) -> dict[str, float]:
    """USD-based fiat table from open.er-api.com. Raises on any problem."""
    res = await client.get("https://open.er-api.com/v6/latest/USD")
    res.raise_for_status()
    payload = res.json()
    table = payload.get("rates") or {}
    out: dict[str, float] = {}
    for code in FIAT_CODES:
        rate = table.get(code)
        if isinstance(rate, (int, float)) and rate > 0:
            out[code] = float(rate)
    if len(out) < len(FIAT_CODES) - 2:  # tolerate a couple missing codes
        raise ValueError(f"fiat table too sparse ({len(out)} codes)")
    out["USD"] = 1.0
    return out


async def _fetch_crypto(client: httpx.AsyncClient) -> dict[str, float]:
    """Crypto units-per-USD from CoinGecko. Raises on any problem."""
    ids = ",".join(CRYPTO_IDS)
    res = await client.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": ids, "vs_currencies": "usd"},
    )
    res.raise_for_status()
    payload = res.json()
    out: dict[str, float] = {}
    for gecko_id, code in CRYPTO_IDS.items():
        usd_price = (payload.get(gecko_id) or {}).get("usd")
        if isinstance(usd_price, (int, float)) and usd_price > 0:
            out[code] = 1.0 / float(usd_price)
    if not out:
        raise ValueError("crypto table empty")
    return out


async def _fetch_live() -> dict[str, float]:
    """Fetch + merge fiat and crypto. Crypto failures degrade to static
    crypto rates rather than failing the whole refresh (fiat is the
    load-bearing part of the table)."""
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT_S,
        headers={"User-Agent": "loupe-fx/1.0"},
    ) as client:
        fiat = await _fetch_fiat(client)
        try:
            crypto = await _fetch_crypto(client)
        except Exception:
            logger.warning("fx: crypto fetch failed; using static crypto rates")
            crypto = {c: STATIC_RATES[c] for c in CRYPTO_IDS.values()}
    return {**fiat, **crypto}


def _complete(rates: dict[str, float]) -> dict[str, float]:
    """Guarantee every supported code is present (fill from static)."""
    merged = dict(STATIC_RATES)
    merged.update(rates)
    return merged


async def get_rates(*, force_refresh: bool = False) -> dict[str, Any]:
    """The FX table both clients render prices with.

    Returns ``{"base": "USD", "as_of": iso, "source": src, "rates": {...}}``
    where source is ``live`` (fresh fetch), ``cached`` (L2 hit) or
    ``static`` (everything else failed). Never raises.
    """
    if not force_refresh:
        cached = await kv_get(_CACHE_KEY)
        if cached:
            try:
                doc = json.loads(cached)
                doc["source"] = "cached"
                return doc
            except Exception:
                pass  # corrupt entry — fall through to refresh

    try:
        rates = _complete(await _fetch_live())
        doc = {
            "base": "USD",
            "as_of": datetime.now(UTC).isoformat(),
            "source": "live",
            "rates": rates,
        }
        await kv_set(_CACHE_KEY, json.dumps(doc), ttl_seconds=_CACHE_TTL_SECONDS)
        return doc
    except Exception:
        logger.warning("fx: live fetch failed; serving static snapshot")
        return {
            "base": "USD",
            "as_of": None,
            "source": "static",
            "rates": dict(STATIC_RATES),
        }


__all__ = ["CRYPTO_IDS", "FIAT_CODES", "STATIC_RATES", "get_rates"]
