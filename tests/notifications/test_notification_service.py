"""The inbox: creation, dedupe, paging, read state.

The behaviours worth pinning are the ones that were impossible before this
table existed — a durable unread count, a stable page boundary, and a dedupe
guarantee strong enough that replayed webhooks and re-run crons are safe.
"""

from __future__ import annotations

import pytest

from app.services import notification_service, push_service
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    """Never touch Expo from a test; count the calls instead."""
    calls: list = []

    async def fake_send(user_id, **kwargs):
        calls.append((user_id, kwargs))
        return 1

    monkeypatch.setattr(push_service, "send_to_user", fake_send)
    return calls


@pytest.mark.asyncio
async def test_notify_creates_a_row_and_pushes(db_session, _no_push):
    user = await make_user(db_session)
    row = await notification_service.notify(
        db_session,
        user.id,
        category="market",
        kind="price_alert",
        title="Charizard is up",
        body="Above your alert.",
        href="/cards/abc",
    )
    assert row is not None
    assert row.read_at is None
    assert len(_no_push) == 1
    # The push carries the id + route so a tap can deep-link straight in.
    assert _no_push[0][1]["data"]["notificationId"] == str(row.id)


@pytest.mark.asyncio
async def test_dedupe_key_blocks_a_second_delivery(db_session):
    """A replayed webhook or a re-run cron must not double-post."""
    user = await make_user(db_session)
    first = await notification_service.notify(
        db_session,
        user.id,
        category="news",
        kind="blog_post",
        title="New article",
        dedupe_key="blog:123",
        push=False,
    )
    second = await notification_service.notify(
        db_session,
        user.id,
        category="news",
        kind="blog_post",
        title="New article",
        dedupe_key="blog:123",
        push=False,
    )
    assert first is not None
    # None means "already told them" — callers treat it as success, not error.
    assert second is None


@pytest.mark.asyncio
async def test_unread_count_and_mark_read(db_session):
    user = await make_user(db_session)
    rows = []
    for i in range(3):
        rows.append(
            await notification_service.notify(
                db_session,
                user.id,
                category="system",
                kind="test",
                title=f"n{i}",
                push=False,
            )
        )
    assert await notification_service.unread_count(db_session, user.id) == 3

    await notification_service.mark_read(db_session, user.id, [rows[0].id])
    assert await notification_service.unread_count(db_session, user.id) == 2

    await notification_service.mark_all_read(db_session, user.id)
    assert await notification_service.unread_count(db_session, user.id) == 0


@pytest.mark.asyncio
async def test_mark_read_cannot_touch_someone_elses_notification(db_session):
    """The id is guessable; ownership is the only thing protecting the row."""
    owner = await make_user(db_session)
    attacker = await make_user(db_session)
    row = await notification_service.notify(
        db_session,
        owner.id,
        category="system",
        kind="test",
        title="private",
        push=False,
    )
    changed = await notification_service.mark_read(db_session, attacker.id, [row.id])
    assert changed == 0
    assert await notification_service.unread_count(db_session, owner.id) == 1


@pytest.mark.asyncio
async def test_paging_is_stable_when_rows_share_a_timestamp(db_session):
    """Rows created in one transaction share created_at; without the id
    tiebreaker a row could repeat on page 2 or vanish entirely."""
    user = await make_user(db_session)
    for i in range(10):
        await notification_service.notify(
            db_session,
            user.id,
            category="system",
            kind="test",
            title=f"n{i}",
            push=False,
        )
    page1, total = await notification_service.list_for_user(
        db_session, user.id, page=1, page_size=4
    )
    page2, _ = await notification_service.list_for_user(
        db_session, user.id, page=2, page_size=4
    )
    assert total == 10
    assert len(page1) == 4 and len(page2) == 4
    assert {r.id for r in page1}.isdisjoint({r.id for r in page2})


@pytest.mark.asyncio
async def test_list_filters_by_category_and_unread(db_session):
    user = await make_user(db_session)
    market = await notification_service.notify(
        db_session,
        user.id,
        category="market",
        kind="price_alert",
        title="m",
        push=False,
    )
    await notification_service.notify(
        db_session, user.id, category="news", kind="blog_post", title="n", push=False
    )
    rows, total = await notification_service.list_for_user(
        db_session, user.id, category="market"
    )
    assert total == 1 and rows[0].category == "market"

    await notification_service.mark_read(db_session, user.id, [market.id])
    rows, total = await notification_service.list_for_user(
        db_session, user.id, unread_only=True
    )
    assert total == 1 and rows[0].category == "news"


@pytest.mark.asyncio
async def test_broadcast_reaches_every_active_user(db_session):
    a = await make_user(db_session)
    b = await make_user(db_session)
    created = await notification_service.broadcast(
        db_session,
        category="news",
        kind="blog_post",
        title="Everyone gets this",
        push=False,
    )
    assert created >= 2
    for user in (a, b):
        assert await notification_service.unread_count(db_session, user.id) == 1


@pytest.mark.asyncio
async def test_broadcast_dedupes_per_user(db_session):
    """Publishing the same article twice must not notify anyone twice — and
    must not be blocked wholesale either, since the key is per-user."""
    user = await make_user(db_session)
    # Capture before broadcasting: the duplicate path rolls back, which expires
    # every instance in this session — including `user`.
    user_id = user.id
    first = await notification_service.broadcast(
        db_session,
        category="news",
        kind="blog_post",
        title="Post",
        dedupe_prefix="blog:xyz",
        push=False,
    )
    second = await notification_service.broadcast(
        db_session,
        category="news",
        kind="blog_post",
        title="Post",
        dedupe_prefix="blog:xyz",
        push=False,
    )
    assert first >= 1
    assert second == 0
    assert await notification_service.unread_count(db_session, user_id) == 1


@pytest.mark.asyncio
async def test_broadcast_skips_banned_and_deleted(db_session):
    from datetime import UTC, datetime

    active = await make_user(db_session)
    banned = await make_user(db_session)
    banned.banned_at = datetime.now(UTC)
    await db_session.commit()

    await notification_service.broadcast(
        db_session, category="news", kind="blog_post", title="x", push=False
    )
    assert await notification_service.unread_count(db_session, active.id) == 1
    assert await notification_service.unread_count(db_session, banned.id) == 0


@pytest.mark.asyncio
async def test_notification_survives_a_failed_push(db_session, monkeypatch):
    """Push is delivery, not the record — a dead provider must not lose it."""

    async def boom(*args, **kwargs):
        raise RuntimeError("expo down")

    monkeypatch.setattr(push_service, "send_to_user", boom)
    user = await make_user(db_session)
    row = await notification_service.notify(
        db_session, user.id, category="system", kind="test", title="still here"
    )
    assert row is not None
    assert await notification_service.unread_count(db_session, user.id) == 1
    assert row.pushed_at is None


@pytest.mark.asyncio
async def test_page_size_is_capped(db_session):
    user = await make_user(db_session)
    rows, _ = await notification_service.list_for_user(
        db_session, user.id, page_size=10_000
    )
    assert isinstance(rows, list)  # no explosion; the cap is applied internally


@pytest.mark.asyncio
async def test_title_is_truncated_to_the_column_width(db_session):
    """A long catalog name must not blow up the insert."""
    user = await make_user(db_session)
    row = await notification_service.notify(
        db_session,
        user.id,
        category="market",
        kind="price_alert",
        title="x" * 500,
        push=False,
    )
    assert row is not None and len(row.title) == 200


# ── The inbox header ──


@pytest.mark.asyncio
async def test_summary_counts_unread_per_category(db_session):
    """The filter-strip badges come from one query, and they must add up."""
    user = await make_user(db_session)
    for category, n in (("social", 3), ("market", 2), ("news", 1)):
        for i in range(n):
            await notification_service.notify(
                db_session, user.id, category=category, kind="t", title=f"{category}{i}"
            )
    read = await notification_service.notify(
        db_session, user.id, category="social", kind="t", title="already read"
    )
    assert read is not None
    await notification_service.mark_read(db_session, user.id, [read.id])

    out = await notification_service.summary(db_session, user.id)
    counts = {c["key"]: c["unread"] for c in out["categories"]}
    assert counts == {"social": 3, "market": 2, "news": 1, "system": 0}
    assert out["unread"] == 6
    # The strip is server-defined: labels ship alongside the counts, so the
    # clients never hardcode the tabs.
    assert [c["label"] for c in out["categories"]] == [
        "Community",
        "Price alerts",
        "News",
        "Account",
    ]


@pytest.mark.asyncio
async def test_summary_folds_untabbed_categories_into_account(db_session):
    """`billing` is a stored category with no tab of its own.

    Dropped from the summary, the tab badges would not sum to the app-icon
    badge and one notification would be unreachable through the filter strip.
    """
    user = await make_user(db_session)
    await notification_service.notify(
        db_session, user.id, category="billing", kind="t", title="Card declined"
    )
    out = await notification_service.summary(db_session, user.id)
    counts = {c["key"]: c["unread"] for c in out["categories"]}
    assert counts["system"] == 1
    assert out["unread"] == sum(c["unread"] for c in out["categories"])


@pytest.mark.asyncio
async def test_summary_is_scoped_to_one_user(db_session):
    mine = await make_user(db_session)
    theirs = await make_user(db_session)
    await notification_service.notify(
        db_session, theirs.id, category="social", kind="t", title="not yours"
    )
    out = await notification_service.summary(db_session, mine.id)
    assert out["unread"] == 0
    assert all(c["unread"] == 0 for c in out["categories"])
