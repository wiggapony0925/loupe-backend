"""WebSocket endpoint for live scan progress updates."""

from __future__ import annotations

import asyncio
import json
import uuid

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from app.auth.jwt import verify_token
from app.platform.cache_config import SCAN_PUBSUB_CHANNEL
from app.clients.redis_client import get_redis
from app.db import get_sessionmaker
from app.models.user import User
from app.utils.logger import get_logger
from app.platform.ws_manager import get_manager, ws_envelope

router = APIRouter(tags=["ws"])
logger = get_logger("routers.ws")


async def _authenticate(token: str) -> User | None:
    try:
        claims = verify_token(token, expected_type="access")
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        logger.info("WS auth failed: %s", exc)
        return None
    sm = get_sessionmaker()
    async with sm() as db:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is not None and user.deleted_at is None:
            return user
    return None


async def _redis_relay(user_id: uuid.UUID, ws: WebSocket) -> None:
    """Forward Redis pub/sub messages for this user to the connected WS."""
    redis = await get_redis()
    pubsub = None
    if not hasattr(redis, "pubsub"):  # in-memory stub
        return
    try:
        pubsub = redis.pubsub()
        channel = SCAN_PUBSUB_CHANNEL.format(user_id=user_id)
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8")
            try:
                await ws.send_text(data)
            except Exception:
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover
        logger.debug("Redis relay error for %s: %s", user_id, exc)
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception:
                pass


@router.websocket("/ws/scans")
async def scan_progress_socket(
    ws: WebSocket,
    token: str = Query(..., description="Access token (JWT) for auth."),
) -> None:
    """Push real-time scan progress events to the authenticated user."""
    user = await _authenticate(token)
    if user is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    manager = get_manager()
    await manager.connect(str(user.id), ws)
    relay_task = asyncio.create_task(_redis_relay(user.id, ws))
    try:
        await ws.send_text(json.dumps(ws_envelope("hello", {"user_id": str(user.id)})))
        while True:
            # Keep the connection alive; treat incoming frames as pings.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.info("WS error for %s: %s", user.id, exc)
    finally:
        relay_task.cancel()
        try:
            await relay_task
        except (asyncio.CancelledError, Exception):
            pass
        await manager.disconnect(str(user.id), ws)


__all__ = ["router"]
