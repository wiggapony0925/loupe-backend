"""HTTP & WebSocket routers for loupe-backend."""

from app.routers import (
    auth,
    cards,
    collections,
    grades,
    prices,
    scanners,
    scans,
    sets,
    system,
    users,
    ws,
)

__all__ = [
    "auth",
    "cards",
    "collections",
    "grades",
    "prices",
    "scanners",
    "scans",
    "sets",
    "system",
    "users",
    "ws",
]
