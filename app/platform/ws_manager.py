"""In-process WebSocket connection manager keyed by ``user_id``.

Every frame is wrapped in the universal :func:`ws_envelope`
``{type, ts, request_id, data}`` so the frontend can validate WS messages
against a single shape — mirroring the HTTP response envelope.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

from app.platform.request_context import get_request_id, new_request_id
from app.utils.logger import get_logger
from app.utils.time import utcnow

_log = get_logger("ws")


def ws_envelope(type_: str, data: Any) -> dict[str, Any]:
    """Build the universal WS frame envelope ``{type, ts, request_id, data}``."""
    ts = utcnow().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "type": type_,
        "ts": ts,
        "request_id": get_request_id() or new_request_id(),
        "data": data,
    }


class ConnectionManager:
    """Track active WebSocket connections by ``user_id`` and broadcast to them."""

    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        """Accept a WebSocket and register it for *user_id*."""
        await ws.accept()
        async with self._lock:
            self._conns[user_id].add(ws)
        _log.info("ws connect user=%s (total=%d)", user_id, len(self._conns[user_id]))

    async def disconnect(self, user_id: str, ws: WebSocket) -> None:
        """Remove a WebSocket from the manager."""
        async with self._lock:
            self._conns.get(user_id, set()).discard(ws)
            if user_id in self._conns and not self._conns[user_id]:
                del self._conns[user_id]
        _log.info("ws disconnect user=%s", user_id)

    async def broadcast(
        self,
        user_id: str,
        message: dict[str, Any],
        *,
        type_: str = "scan.progress",
    ) -> int:
        """Send *message* to all sockets currently held for *user_id*.

        The payload is wrapped in :func:`ws_envelope` so every frame the
        frontend receives shares the ``{type, ts, request_id, data}`` shape.
        Callers may override the envelope ``type`` for non-progress events.
        Pre-wrapped envelopes (containing both ``type`` and ``data``) are
        forwarded as-is so worker code can emit precise event types.
        """
        if isinstance(message, dict) and "type" in message and "data" in message:
            envelope = message
        else:
            envelope = ws_envelope(type_, message)
        async with self._lock:
            sockets = list(self._conns.get(user_id, ()))
        delivered = 0
        for ws in sockets:
            try:
                await ws.send_json(envelope)
                delivered += 1
            except Exception as exc:
                _log.warning("ws send failed user=%s: %s", user_id, exc)
        return delivered

    def active_users(self) -> int:
        """Return the number of distinct connected users."""
        return len(self._conns)


_manager = ConnectionManager()


def get_manager() -> ConnectionManager:
    """Return the process-wide :class:`ConnectionManager`."""
    return _manager


__all__ = ["ConnectionManager", "get_manager", "ws_envelope"]
