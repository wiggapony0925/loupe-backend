"""Durable key-value cache rows (L2 behind Redis / in-process cache).

Production has no reachable Redis (see DEPLOY.md §2) — the in-process
fallback is wiped on every Cloud Run instance recycle, so "cached" catalog
surfaces were effectively uncached. This table is the durable second tier:
small JSON payloads with an expiry, shared by every instance, surviving
restarts. Reads/writes are best-effort (a cache must never take a request
down) — see :mod:`app.platform.cache_l2`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class KvCacheEntry(Base):
    __tablename__ = "kv_cache"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


__all__ = ["KvCacheEntry"]
