"""Live price feed — ticks reach card OWNERS, and only owners.

`record_price_observation` publishes a `price.tick` frame whenever a
persisted observation actually changes a card's price. With the in-memory
Redis stub (the test topology), delivery goes through the local
connection manager — these tests capture it with a fake manager.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.services.market import price_feed_service
from app.tasks import price_snapshot
from tests.factories import make_card


class _FakeManager:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    async def broadcast(self, user_id: str, message: dict, **_: object) -> int:
        self.frames.append((user_id, message))
        return 1


class _StubRedis:
    """``publish`` but no ``pubsub`` → the feed treats this as the in-memory
    stub and delivers through the local connection manager (what these tests
    capture). Mirrors :class:`app.platform.redis_client._InMemoryRedis`."""

    async def publish(self, channel: str, message: str) -> int:  # pragma: no cover
        return 0


@pytest.fixture(autouse=True)
def _force_inmemory_delivery(monkeypatch):
    """Pin the feed to its in-memory topology. CI provides real Redis, whose
    pub/sub path bypasses the local manager — so without this the manager-path
    assertions below only pass where Redis is absent (e.g. a dev laptop)."""

    async def _stub() -> _StubRedis:
        return _StubRedis()

    monkeypatch.setattr(price_feed_service, "get_redis", _stub)


async def _mk_user(db, label: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"feed-{label}-{uuid.uuid4().hex[:8]}@test.dev",
        display_name=f"Feed {label}",
    )
    db.add(user)
    await db.commit()
    return user


@pytest.mark.anyio
async def test_tick_targets_owners_only(db_session, monkeypatch):
    owner = await _mk_user(db_session, "owner")
    await _mk_user(db_session, "bystander")
    card = await make_card(db_session, name="Umbreon VMAX")
    db_session.add(
        GradedCard(
            user_id=owner.id,
            card_id=card.id,
            grade=10,
            house=GradeHouseEnum.psa,
        )
    )
    await db_session.commit()

    fake = _FakeManager()
    monkeypatch.setattr(price_feed_service, "get_manager", lambda: fake)

    published = await price_feed_service.publish_price_tick(db_session, card, 123.45)

    assert published == 1
    assert len(fake.frames) == 1
    user_id, frame = fake.frames[0]
    assert user_id == str(owner.id)
    # Universal WS envelope + tick payload.
    assert frame["type"] == "price.tick"
    assert "ts" in frame and "request_id" in frame
    assert frame["data"]["cardId"] == str(card.id)
    assert frame["data"]["cardName"] == "Umbreon VMAX"
    assert frame["data"]["priceUsd"] == 123.45


@pytest.mark.anyio
async def test_unowned_card_publishes_nothing(db_session, monkeypatch):
    card = await make_card(db_session, name="Browse-only card")
    fake = _FakeManager()
    monkeypatch.setattr(price_feed_service, "get_manager", lambda: fake)

    assert await price_feed_service.publish_price_tick(db_session, card, 9.99) == 0
    assert fake.frames == []


@pytest.mark.anyio
async def test_observation_ticks_on_change_and_stays_silent_on_noop(
    db_session, monkeypatch
):
    owner = await _mk_user(db_session, "obs")
    card = await make_card(db_session, name="Ticker")
    db_session.add(
        GradedCard(
            user_id=owner.id,
            card_id=card.id,
            grade=9,
            house=GradeHouseEnum.loupe,
        )
    )
    await db_session.commit()

    fake = _FakeManager()
    monkeypatch.setattr(price_feed_service, "get_manager", lambda: fake)

    # First observation of the day → history changes → one tick.
    assert await price_snapshot.record_price_observation(str(card.id), 50.0) is True
    assert len(fake.frames) == 1
    assert fake.frames[0][1]["data"]["priceUsd"] == 50.0

    # Same price again today → no-op → no tick.
    assert await price_snapshot.record_price_observation(str(card.id), 50.0) is False
    assert len(fake.frames) == 1

    # Intraday MOVE → today's point updates → second tick.
    assert await price_snapshot.record_price_observation(str(card.id), 55.0) is True
    assert len(fake.frames) == 2
    assert fake.frames[1][1]["data"]["priceUsd"] == 55.0
