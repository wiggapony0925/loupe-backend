"""Helpers for instrumenting outbound HTTP calls to upstream providers.

Use :func:`log_upstream_call` as an async context manager around each call
so latency, status, and provider names land in the structured logs and
become aggregatable in any log-search tool::

    async with log_upstream_call("pokemontcg", "GET", url) as ctx:
        resp = await client.get(url)
        ctx.status = resp.status_code

The block always emits exactly one log line (INFO on 2xx/3xx, WARNING on
4xx, ERROR on 5xx or unhandled exception).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.utils.logger import get_logger

_log = get_logger("upstream")


@dataclass
class UpstreamCallContext:
    provider: str
    method: str
    url: str
    status: int | None = None
    bytes_in: int | None = None
    note: str | None = None


@asynccontextmanager
async def log_upstream_call(provider: str, method: str, url: str):
    """Emit one structured log line per upstream HTTP call.

    Mutate the yielded :class:`UpstreamCallContext` to record the response
    ``status`` (and optionally ``bytes_in``/``note``). Exceptions are
    re-raised after being logged at ERROR.
    """
    ctx = UpstreamCallContext(provider=provider, method=method, url=url)
    start = time.perf_counter()
    try:
        yield ctx
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _log.exception(
            "upstream %s %s %s -> EXC (%.1fms): %s",
            provider,
            method,
            url,
            elapsed_ms,
            exc,
            extra={
                "event": "upstream.call",
                "upstream_provider": provider,
                "http_method": method,
                "upstream_url": url,
                "latency_ms": round(elapsed_ms, 1),
                "outcome": "exception",
            },
        )
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    status = ctx.status or 0
    if status >= 500:
        level = "error"
    elif status >= 400:
        level = "warning"
    else:
        level = "info"
    log_fn = getattr(_log, level)
    log_fn(
        "upstream %s %s %s -> %d (%.1fms)%s",
        provider,
        method,
        url,
        status,
        elapsed_ms,
        f" [{ctx.note}]" if ctx.note else "",
        extra={
            "event": "upstream.call",
            "upstream_provider": provider,
            "http_method": method,
            "upstream_url": url,
            "http_status": status,
            "latency_ms": round(elapsed_ms, 1),
            "bytes_in": ctx.bytes_in,
            "outcome": "ok" if status < 400 else "error",
        },
    )


__all__ = ["UpstreamCallContext", "log_upstream_call"]
