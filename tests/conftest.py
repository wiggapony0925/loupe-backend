"""Pytest fixtures: app boot, async DB, HTTP client, auth helpers."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6390/0")  # unreachable → stub
os.environ.setdefault("S3_ENDPOINT_URL", "")
os.environ.setdefault("S3_BUCKET", "loupe-test")
# Force the S3 stub during tests: empty creds make get_s3_client() pick
# the in-memory backend regardless of what the dev shell or .env exports.
for _k in (
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
):
    os.environ.pop(_k, None)
os.environ["S3_ACCESS_KEY_ID"] = ""
os.environ["S3_SECRET_ACCESS_KEY"] = ""
# Force email OFF during tests: a developer's .env may hold a real
# RESEND_API_KEY (local sends), and tests must never hit the provider —
# email-path tests opt back in by monkeypatching settings + the HTTP client.
os.environ["RESEND_API_KEY"] = ""
os.environ["NOTIFICATIONS_FROM_EMAIL"] = ""
os.environ["RESEND_WEBHOOK_SECRET"] = ""

from app.config import reload_settings  # noqa: E402
from app.db import Base, get_db, reset_engine  # noqa: E402

reload_settings()

# Drop any S3 client cached at import time so it picks up the test config.
try:
    from app.platform.s3 import reset_s3_client

    reset_s3_client()
except Exception:
    pass


@pytest.fixture(autouse=True)
def _reset_process_singletons():
    """Reset process-wide singletons around every test so state never leaks
    across tests. Critical under CI's per-test event loops (the session
    ``event_loop`` fixture is ignored by newer pytest-asyncio): the in-memory
    Redis client and the fire-and-forget background-task sets would otherwise
    carry a prior test's data/tasks into the next. Sync + direct assignment, so
    it's event-loop-agnostic."""

    def _reset() -> None:
        from app.platform import cache_swr, redis_client
        from app.services.catalog import card_search_service, carousel_service

        redis_client._client = None
        for taskset in (
            cache_swr._bg_tasks,
            carousel_service._bg_tasks,
            card_search_service._price_bg_tasks,
        ):
            for task in list(taskset):
                task.cancel()
            taskset.clear()
        carousel_service._inflight.clear()
        carousel_service._last_attempt.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _offline_provider_registry():
    """Serve an EMPTY provider registry to every test by default.

    The real registry fans out to live market-data APIs (PriceCharting, eBay,
    130point, pokemontcg.io, …) and the developer's ``.env`` carries real API
    keys, so any unpatched path through ``get_registry()`` — e.g. ``get_card``'s
    market-chain price enrichment for a card with no embedded pricing — hits
    the actual network mid-test. That is slow, burns paid quota, and makes
    tests nondeterministic: a card the test declared priceless comes back with
    today's real market price whenever the API answers within the fan-out
    timeout.

    Tests that exercise the registry itself opt back in with
    ``reset_registry()`` (clears this override), and service tests that need a
    fake keep monkeypatching their module's ``get_registry`` binding as before.
    """
    from app.integrations.registry import ProviderRegistry, set_registry_override

    set_registry_override(ProviderRegistry(providers=[]))
    yield
    set_registry_override(None)


@pytest_asyncio.fixture(autouse=True)
async def _flush_redis_between_tests():
    """Flush the Redis DB around every async test so cache keys never leak
    across tests. Matters in CI, which runs against a REAL Redis service (keys
    persist within a run); the in-memory stub also implements flushdb."""

    async def _flush() -> None:
        from app.platform.redis_client import get_redis

        try:
            client = await get_redis()
            if hasattr(client, "flushdb"):
                await client.flushdb()
        except Exception:
            pass

    await _flush()
    yield
    await _flush()


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory engine per test, with all tables created."""
    await reset_engine()
    from app.db import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    except Exception:
        pass
    await reset_engine()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncIterator:
    from app.db import get_sessionmaker

    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
    finally:
        try:
            await session.close()
        except Exception:
            pass


@pytest_asyncio.fixture
async def client(db_engine):
    """httpx AsyncClient bound to the FastAPI app via ASGI transport."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    app = create_app()

    async def _override_get_db() -> AsyncIterator:
        from app.db import get_sessionmaker

        sm = get_sessionmaker()
        async with sm() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def created_user(db_session):
    from app.models.user import User, UserSettings

    user = User(
        email=f"tester+{uuid.uuid4().hex[:8]}@example.com",
        display_name="Tester",
        apple_subject=f"apple-{uuid.uuid4().hex}",
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id))
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(created_user) -> dict[str, str]:
    from app.auth.jwt import issue_token

    token, _ = issue_token(created_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------- helpers


def assert_envelope_ok(resp, expected_status: int = 200):
    """Assert *resp* is an envelope success and return ``data``.

    Validates the envelope shape (``data`` / ``meta`` / ``pagination`` /
    ``error``) and the required meta fields.  Returns ``body["data"]`` for
    convenient inline use::

        body = assert_envelope_ok(await client.get("/v1/me", headers=h))
        assert body["email"] == "..."
    """
    assert resp.status_code == expected_status, (
        f"unexpected status {resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    assert isinstance(payload, dict), f"envelope must be an object, got {type(payload)}"
    assert "data" in payload, f"envelope missing 'data': {payload}"
    assert "meta" in payload, f"envelope missing 'meta': {payload}"
    meta = payload["meta"]
    assert isinstance(meta, dict)
    for key in ("request_id", "timestamp", "version"):
        assert key in meta, f"meta missing '{key}': {meta}"
    assert payload.get("error") is None, f"expected no error, got {payload['error']}"
    return payload["data"]


def assert_envelope_error(
    resp, *, expected_status: int, expected_code: str | None = None
):
    """Assert *resp* is an envelope error and return the ``error`` block."""
    assert resp.status_code == expected_status, (
        f"unexpected status {resp.status_code}: {resp.text}"
    )
    payload = resp.json()
    assert isinstance(payload, dict)
    assert "error" in payload and payload["error"] is not None, payload
    assert "meta" in payload
    err = payload["error"]
    assert payload.get("data") is None
    if expected_code is not None:
        assert err.get("code") == expected_code, err
    return err


def envelope_pagination(resp) -> dict:
    """Assert *resp* is a paginated envelope and return the ``pagination`` block."""
    payload = resp.json()
    assert payload.get("pagination") is not None, (
        f"expected pagination block: {payload}"
    )
    return payload["pagination"]
