"""Live price feed — fan price ticks out to the card's OWNERS over WebSocket.

Every persisted price observation (see ``record_price_observation``) calls
:func:`publish_price_tick`. The frame reaches each owner's ``/ws/prices``
socket so the vault, portfolio chart, and card detail can move in real
time instead of waiting for the next poll.

Delivery topology (exactly-once by construction):

* **Real Redis** — publish to the per-user channel only. EVERY API
  instance relays its own connected sockets from that channel (including
  the instance that published), so a local broadcast on top would
  double-send to same-instance users.
* **In-memory stub** (dev / tests / broker outage) — broadcast to the
  local connection manager only; there is exactly one process, so local
  IS total.

Ticks originate in both the API process (serve-path observations, the
owned-card freshness sweep) and the arq worker (nightly backfill) — the
Redis path is what carries worker-origin ticks to API-held sockets.

Best effort everywhere: a feed failure must never break the price write
that triggered it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.grade import GradedCard
from app.platform.cache_config import PRICE_PUBSUB_CHANNEL
from app.platform.redis_client import get_redis
from app.platform.ws_manager import get_manager, ws_envelope
from app.utils.logger import get_logger

_log = get_logger("services.price_feed")


async def publish_price_tick(
    session: AsyncSession, card: Card, price_usd: float
) -> int:
    """Push a ``price.tick`` frame to every owner of *card*.

    Returns the number of owner channels the tick was published to
    (0 when nobody owns the card — the common case for browse traffic).
    Never raises.
    """
    try:
        owner_ids = (
            (
                await session.execute(
                    select(GradedCard.user_id)
                    .where(
                        GradedCard.card_id == card.id,
                        GradedCard.deleted_at.is_(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not owner_ids:
            return 0

        frame: dict[str, Any] = ws_envelope(
            "price.tick",
            {
                "cardId": str(card.id),
                "cardName": card.name,
                "priceUsd": float(price_usd),
            },
        )
        redis = await get_redis()
        use_redis = hasattr(redis, "publish") and hasattr(redis, "pubsub")
        payload = json.dumps(frame) if use_redis else ""
        manager = get_manager()

        published = 0
        for uid in owner_ids:
            try:
                if use_redis:
                    await redis.publish(
                        PRICE_PUBSUB_CHANNEL.format(user_id=uid), payload
                    )
                else:
                    await manager.broadcast(str(uid), frame)
                published += 1
            except Exception as exc:  # pragma: no cover — per-owner best effort
                _log.debug("price tick publish failed for %s: %s", uid, exc)
        return published
    except Exception:  # pragma: no cover — the feed is always optional
        _log.debug("price tick fan-out failed for %s", card.id, exc_info=True)
        return 0


__all__ = ["publish_price_tick"]
