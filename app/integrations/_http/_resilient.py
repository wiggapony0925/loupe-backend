"""Resilient HTTP helper shared by all third-party catalog clients.

Every outbound call to a public TCG provider (pokemontcg.io, scryfall,
ygoprodeck, …) goes through :func:`request_json`. The helper layers two
production-critical concerns on top of raw ``httpx``:

1. **Circuit breaking** — a named :class:`~app.platform.circuit_breaker.CircuitBreaker`
   per integration. If a provider racks up consecutive failures the
   breaker opens and subsequent calls raise
   :class:`~app.platform.circuit_breaker.CircuitOpenError` *immediately*
   instead of paying the timeout. After a cooldown one probe is allowed
   in to decide whether to close the breaker again.

2. **Expected-404 semantics** — single-resource lookups (``get_card``)
   legitimately return 404 for unknown IDs; that's a clean "miss", not
   an upstream failure. Callers pass ``not_found_ok=True`` and the
   helper returns ``None`` for 404 *without* tripping the breaker.

What counts as a failure
------------------------
* Connection errors / timeouts (``httpx.TransportError``,
  ``httpx.TimeoutException``, ``asyncio.TimeoutError``).
* HTTP 5xx (server fault).
* HTTP 4xx **except** ``404`` when ``not_found_ok=True``. We surface
  4xx as failures because a flapping provider that starts returning
  400/401/403 for valid queries is broken just as surely as one
  returning 503.

Everything else (200/2xx, an expected 404) is recorded as a breaker
success.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.platform.circuit_breaker import get_breaker

# Tuned for third-party TCG providers: 5 consecutive bad calls before
# we cool down for 60s. The fan-out callers (search, trending,
# canonical-resolve) already short-circuit on `CircuitOpenError` so a
# tripped breaker degrades by simply omitting that provider from the
# merged response — the user still gets the other catalogs.
_DEFAULT_THRESHOLD = 5
_DEFAULT_COOLDOWN_S = 60.0


async def request_json(
    *,
    integration: str,
    method: str,
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float,
    not_found_ok: bool = False,
    extra_ok_statuses: tuple[int, ...] = (),
    breaker_threshold: int = _DEFAULT_THRESHOLD,
    breaker_cooldown_s: float = _DEFAULT_COOLDOWN_S,
) -> Any:
    """Issue an HTTP call guarded by the named integration's breaker.

    Parameters
    ----------
    integration:
        Stable label used to look up the circuit breaker (e.g.
        ``"pokemontcg"``, ``"scryfall"``, ``"ygoprodeck"``). Same
        label => same breaker instance.
    not_found_ok:
        When True, a 404 response returns ``None`` and is recorded as
        a breaker success (clean miss, not a fault).
    extra_ok_statuses:
        Additional status codes that should be treated as a normal
        success and returned as an empty payload ``{"data": []}``
        rather than raising. Used for upstreams whose "no results"
        signal is a 4xx (ygoprodeck returns 400 for an unknown card).

    Returns
    -------
    Any
        Parsed JSON body, or ``None`` when ``not_found_ok`` swallows a
        404, or ``{"data": []}`` for an ``extra_ok_statuses`` hit.

    Raises
    ------
    app.platform.circuit_breaker.CircuitOpenError
        Breaker is currently open; caller should degrade.
    httpx.HTTPStatusError, httpx.HTTPError, asyncio.TimeoutError
        Any underlying transport error; the breaker has already
        recorded the failure on the way out.
    """
    breaker = get_breaker(
        integration,
        failure_threshold=breaker_threshold,
        cooldown_s=breaker_cooldown_s,
    )

    # Result is captured inside the guarded block and read after it
    # exits cleanly, so a 404 (or other expected-ok status) is recorded
    # as a breaker *success* rather than tripping the failure counter.
    result: dict[str, Any] = {"value": None}

    async with breaker.guard(), httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.request(
            method,
            url,
            params=dict(params) if params else None,
            headers=dict(headers) if headers else None,
        )
        status = resp.status_code
        if not_found_ok and status == 404:
            result["value"] = None
        elif status in extra_ok_statuses:
            result["value"] = {"data": []}
        else:
            resp.raise_for_status()
            result["value"] = resp.json()

    return result["value"]


__all__ = ["request_json"]
