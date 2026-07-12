"""Per-USER rate limiter — caps costly authed endpoints by account, not IP.

IP limits false-positive on carrier NAT (many mobile users, one IP) and
miss one account scripting through proxies. The user-keyed limiter is the
counterpart for authenticated, vault-walking endpoints.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth.dependencies import user_rate_limit


@pytest.mark.asyncio
async def test_limits_per_user_and_isolates_accounts():
    dep = user_rate_limit(limit=2, window_seconds=60, name="test.endpoint")
    alice = SimpleNamespace(id=uuid.uuid4())
    bob = SimpleNamespace(id=uuid.uuid4())

    # Alice's two allowed hits pass; the third is a 429 with Retry-After.
    await dep(user=alice)
    await dep(user=alice)
    with pytest.raises(HTTPException) as exc:
        await dep(user=alice)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"

    # Bob is unaffected by Alice's exhaustion — buckets key on user id.
    await dep(user=bob)


@pytest.mark.asyncio
async def test_independent_limiters_do_not_share_buckets():
    a = user_rate_limit(limit=1, window_seconds=60, name="endpoint.a")
    b = user_rate_limit(limit=1, window_seconds=60, name="endpoint.b")
    user = SimpleNamespace(id=uuid.uuid4())

    await a(user=user)
    # Exhausting endpoint A must not consume endpoint B's budget.
    await b(user=user)
    with pytest.raises(HTTPException):
        await a(user=user)
