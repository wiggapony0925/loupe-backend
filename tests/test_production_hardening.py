"""Production-hardening guards (concurrency, validators, limits, JWT, rate limit).

These tests cover the cross-cutting safety nets added in the
production-readiness pass — none of them belong to a single feature,
but every one is something that has historically taken down apps in
the wild.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import pytest

# Ensure every model the resolver writes to is imported before the
# ``db_engine`` fixture calls ``Base.metadata.create_all`` — otherwise
# the in-memory SQLite schema is missing ``card_external_refs`` and the
# concurrency test fails with "no such table".
from app.models import card as _card  # noqa: F401
from app.models import card_external_ref as _cer  # noqa: F401


# ---------------------------------------------------------------------------
# Batch A — concurrency: same upstream_id resolved twice never throws
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_local_card_is_idempotent(db_session):
    """Two sequential ensure_local_card calls for the same upstream id
    return the same Card — no IntegrityError leaks, no duplicate refs.

    We can't easily reproduce a true thread race against an in-memory
    SQLite session, but idempotence on the happy path catches the bulk
    of regressions and proves the early-return ref lookup works.
    """
    from app.services import card_resolver_service

    unified = {
        "tcg": "pokemon",
        "name": "Charizard",
        "number": "4",
        "rarity": "Holo Rare",
        "year": 1999,
        "set": {"code": "base1", "name": "Base Set"},
    }
    upstream_id = f"pokemontcg:test-{uuid.uuid4().hex[:8]}"

    a = await card_resolver_service.ensure_local_card(
        db_session, upstream_id=upstream_id, unified=unified
    )
    await db_session.commit()
    b = await card_resolver_service.ensure_local_card(
        db_session, upstream_id=upstream_id, unified=unified
    )
    await db_session.commit()

    assert a is not None and b is not None
    assert a.id == b.id, "second call must return the same local Card"


# ---------------------------------------------------------------------------
# Batch B — Pydantic input validators
# ---------------------------------------------------------------------------


def test_grade_create_rejects_future_purchase_date():
    from datetime import date, timedelta

    from pydantic import ValidationError

    from app.schemas.grade import GradedCardCreate

    with pytest.raises(ValidationError) as exc:
        GradedCardCreate(
            upstream_id="pokemontcg:base1-4",
            grade=Decimal("9.5"),
            purchase_date=date.today() + timedelta(days=1),
        )
    assert "future" in str(exc.value).lower()


def test_grade_create_rejects_absurd_value():
    from pydantic import ValidationError

    from app.schemas.grade import GradedCardCreate

    with pytest.raises(ValidationError):
        GradedCardCreate(
            upstream_id="pokemontcg:base1-4",
            grade=Decimal("9.5"),
            estimated_value_usd=Decimal("99999999999"),
        )


def test_grade_create_rejects_out_of_range_subgrade():
    from pydantic import ValidationError

    from app.schemas.grade import GradedCardCreate

    with pytest.raises(ValidationError):
        GradedCardCreate(
            upstream_id="pokemontcg:base1-4",
            grade=Decimal("9.5"),
            subgrades={"centering": 11.0},
        )


def test_grade_create_rejects_non_hex_fingerprint():
    from pydantic import ValidationError

    from app.schemas.grade import GradedCardCreate

    with pytest.raises(ValidationError):
        GradedCardCreate(
            upstream_id="pokemontcg:base1-4",
            grade=Decimal("9.5"),
            fingerprint_hash="not-hex-zzzzz",
        )


def test_grade_create_rejects_malformed_upstream_id():
    from pydantic import ValidationError

    from app.schemas.grade import GradedCardCreate

    with pytest.raises(ValidationError):
        GradedCardCreate(
            upstream_id="no-colon-here",
            grade=Decimal("9.5"),
        )


def test_grade_create_strips_control_chars_from_notes():
    from app.schemas.grade import GradedCardCreate

    g = GradedCardCreate(
        upstream_id="pokemontcg:base1-4",
        grade=Decimal("9.5"),
        notes="hello\x00\x07world",
    )
    assert g.notes == "helloworld"


# ---------------------------------------------------------------------------
# Batch C — list endpoint has a hard cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grades_list_respects_limit_cap(client, auth_headers):
    """Caller can request fewer rows; default cap protects mobile client."""
    resp = await client.get("/v1/grades?limit=10", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    # The list (envelope-wrapped) should be present and small.
    body = resp.json()
    data = body["data"] if isinstance(body, dict) and "data" in body else body
    assert isinstance(data, list)
    assert len(data) <= 10


@pytest.mark.asyncio
async def test_grades_list_rejects_oversized_limit(client, auth_headers):
    """Limit above the documented ceiling returns 422 — never silently fans
    out to the entire portfolio."""
    resp = await client.get("/v1/grades?limit=999999", headers=auth_headers)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Batch D — JWT clock-skew leeway absorbs small drift
# ---------------------------------------------------------------------------


def test_verify_token_tolerates_future_iat_within_leeway():
    """A token whose iat is 5s in the future (e.g. clock drift between
    pods) must still validate, because we configure leeway=30 by default.
    Without leeway the issuer/pod skew issue periodically logged 401s in
    multi-instance prod."""
    import jwt as _jwt

    from app.auth import jwt as auth_jwt
    from app.config import get_settings

    s = get_settings()
    now = int(time.time())
    payload = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(uuid.uuid4()),
        "iat": now + 5,  # 5 seconds in the future — within leeway
        "exp": now + 900,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    token = _jwt.encode(payload, auth_jwt._private_key(), algorithm="RS256")
    decoded = auth_jwt.verify_token(token)
    assert decoded["sub"] == payload["sub"]


def test_verify_token_rejects_iat_outside_leeway():
    """But 5 *minutes* in the future is real abuse — must still fail."""
    import jwt as _jwt

    from app.auth import jwt as auth_jwt
    from app.config import get_settings

    s = get_settings()
    now = int(time.time())
    payload = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(uuid.uuid4()),
        "iat": now + 600,
        "exp": now + 1500,
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    token = _jwt.encode(payload, auth_jwt._private_key(), algorithm="RS256")
    with pytest.raises(_jwt.PyJWTError):
        auth_jwt.verify_token(token)


# ---------------------------------------------------------------------------
# Batch E — rate limiter trips after the configured threshold
# ---------------------------------------------------------------------------


def test_sliding_window_blocks_after_limit():
    """The limiter is the unit under test, not the endpoint — keeps the
    test fast and deterministic (no need to actually hammer FastAPI 61
    times)."""
    from app.rate_limit import _SlidingWindow  # noqa: SLF001

    w = _SlidingWindow(limit=3, window_s=60)
    assert w.hit("ip1") is True
    assert w.hit("ip1") is True
    assert w.hit("ip1") is True
    # Fourth call within the window must be rejected.
    assert w.hit("ip1") is False
    # Different IP is independent.
    assert w.hit("ip2") is True


def test_sliding_window_recovers_after_window():
    """Once the window slides past the old hits the bucket refills."""
    from app.rate_limit import _SlidingWindow  # noqa: SLF001

    # Very short window so the test is fast.
    w = _SlidingWindow(limit=2, window_s=0.2)
    assert w.hit("k") is True
    assert w.hit("k") is True
    assert w.hit("k") is False
    time.sleep(0.25)
    assert w.hit("k") is True


@pytest.mark.asyncio
async def test_resolve_endpoint_is_publicly_reachable(client):
    """Smoke: the resolve endpoint is wired and returns a structured
    response (or 404) — not a 500. Detailed rate-limit behavior is
    covered by the unit tests above."""
    resp = await client.post("/v1/cards/resolve", json={"query": "Charizard"})
    # 200/404 acceptable; 500 means the dependency wiring broke.
    assert resp.status_code in (200, 404, 422), resp.text
