"""Router tests for /v1/admin/scanner — the identify funnel and its scan log.

Every ``POST /v1/cards/identify`` leaves a row behind: what the camera saw, what
we answered, how fast, and what it cost. These endpoints are how the team reads
that back, so the behaviours worth pinning are the windowing (a rolling report
that can't divide by zero on a quiet day), the filters the grid depends on, and
the fact that the whole surface is staff-only — the log holds other people's
scan photos and email addresses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.auth.jwt import issue_token
from app.models.enums import ScanSourceEnum, ScanStatusEnum
from app.models.identification import CardIdentification, IdentificationFeedback
from app.models.scan import ScanJob
from tests.conftest import assert_envelope_error, assert_envelope_ok
from tests.factories import make_user

# A 1x1 JPEG stands in for a scanned frame; only the encoding matters here.
_THUMB_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAg="


@pytest.fixture
async def admin_user(db_session):
    """A Loupe staff account — the portal's caller."""
    user = await make_user(db_session)
    user.is_admin = True
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def admin_headers(admin_user) -> dict[str, str]:
    token, _ = issue_token(admin_user.id, "access")
    return {"Authorization": f"Bearer {token}"}


def _scan(**over) -> CardIdentification:
    """One identify call, with sane defaults; override per test."""
    row = CardIdentification(
        image_sha256=uuid.uuid4().hex,
        ocr_provider="mock",
        tcg_inferred="pokemon",
        primary_source="text",
        top_confidence=0.8,
        latency_ms=120,
        cost_usd=0.0015,
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


# ── Authorization ───────────────────────────────────────────────────────────

_ROUTES = (
    "/v1/admin/scanner",
    "/v1/admin/scanner/trend",
    "/v1/admin/scanner/history",
    f"/v1/admin/scanner/history/{uuid.uuid4()}",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _ROUTES)
async def test_scanner_routes_reject_anonymous_callers(client, path):
    assert_envelope_error(await client.get(path), expected_status=401)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _ROUTES)
async def test_scanner_routes_reject_ordinary_users(client, auth_headers, path):
    """The scan log carries other users' uploaded photos and email addresses,
    so an ordinary signed-in account must not reach any of it — not even the
    aggregate pages, which leak volume and spend."""
    assert_envelope_error(
        await client.get(path, headers=auth_headers), expected_status=403
    )


# ── GET /v1/admin/scanner ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_funnel_splits_scans_by_the_signal_that_matched(
    client, admin_headers, admin_user, db_session
):
    """The pHash fast path resolves a scan without paying for OCR, so its share
    is the headline cost metric on this page."""
    rows = [
        _scan(primary_source="phash", top_confidence=0.95),
        _scan(primary_source="phash", top_confidence=0.9),
        _scan(primary_source="text", top_confidence=0.6),
        _scan(primary_source="none", top_confidence=0.0),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    db_session.add(IdentificationFeedback(identification_id=rows[0].id, correct=True))
    db_session.add(
        ScanJob(
            user_id=admin_user.id,
            status=ScanStatusEnum.complete,
            source=ScanSourceEnum.phone,
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner", headers=admin_headers)
    )

    assert body["total_identifications"] == 4
    assert body["by_source"] == {"phash": 2, "text": 1, "none": 1}
    assert body["fast_path_rate"] == 0.5
    assert body["total_feedback"] == 1
    assert body["correct_feedback"] == 1
    assert body["scans_total"] == 1
    assert body["scans_by_status"] == {"complete": 1}


@pytest.mark.asyncio
async def test_funnel_on_a_silent_window_reports_zeros(client, admin_headers):
    """A brand-new environment (or a genuinely quiet week) has no scans at all.
    The rates are ratios, so this is the case that would divide by zero — it
    must come back as an all-zero report rather than a 500."""
    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner", headers=admin_headers)
    )

    assert body["total_identifications"] == 0
    assert body["by_source"] == {}
    assert body["fast_path_rate"] == 0.0
    assert body["top1_accuracy"] == 0.0
    assert body["total_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_funnel_echoes_the_window_it_measured(client, admin_headers):
    """The chart legend reads ``window_days`` back, so it must reflect the
    request rather than the default."""
    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner?days=7", headers=admin_headers)
    )
    assert body["window_days"] == 7


@pytest.mark.asyncio
async def test_funnel_ignores_scans_older_than_the_window(
    client, admin_headers, db_session
):
    """A rolling window is the whole point: last quarter's accuracy must not
    dilute this week's."""
    db_session.add_all(
        [
            _scan(created_at=datetime.now(UTC) - timedelta(days=1)),
            _scan(created_at=datetime.now(UTC) - timedelta(days=40)),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner?days=7", headers=admin_headers)
    )
    assert body["total_identifications"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 366])
async def test_funnel_window_is_bounded(client, admin_headers, days):
    """The aggregation pulls every row in the window into Python, so the
    window is capped at a year rather than left open-ended."""
    resp = await client.get(f"/v1/admin/scanner?days={days}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/admin/scanner/trend ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trend_fills_every_day_in_the_window(client, admin_headers):
    """Days with no scans still get a point so the chart keeps a continuous
    x-axis instead of drawing a straight line across a gap.

    ``days`` is the number of buckets, not an offset: a 3-day request returns
    exactly 3 dated points, the last of them today.
    """
    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner/trend?days=3", headers=admin_headers)
    )

    assert body["window_days"] == 3
    assert len(body["points"]) == 3
    dates = [p["date"] for p in body["points"]]
    assert dates == sorted(dates), "the series must run oldest → newest"
    assert dates[-1] == datetime.now(UTC).date().isoformat()
    assert all(p["count"] == 0 for p in body["points"])


@pytest.mark.asyncio
async def test_trend_buckets_todays_scans_with_their_speed_and_confidence(
    client, admin_headers, db_session
):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _scan(
                primary_source="phash",
                top_confidence=1.0,
                latency_ms=100,
                created_at=now,
            ),
            _scan(
                primary_source="text",
                top_confidence=0.5,
                latency_ms=300,
                created_at=now,
            ),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner/trend?days=1", headers=admin_headers)
    )

    today = next(p for p in body["points"] if p["date"] == now.date().isoformat())
    assert today["count"] == 2
    assert today["mean_confidence"] == 0.75
    assert today["fast_path_rate"] == 0.5
    assert today["latency_p50_ms"] == 100
    assert today["latency_p95_ms"] == 300


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 181])
async def test_trend_window_is_bounded_tighter_than_the_summary(
    client, admin_headers, days
):
    """The trend materialises one point per day, so it caps at half a year
    even though the summary accepts a full one."""
    resp = await client.get(
        f"/v1/admin/scanner/trend?days={days}", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/admin/scanner/history ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_row_carries_the_frame_and_who_scanned_it(
    client, admin_headers, created_user, db_session
):
    """The grid is a visual log: the scanned frame comes back as a ready-to-
    render data URL, and the scanner's email is joined in so the page needs no
    second fetch."""
    db_session.add(
        _scan(
            user_id=created_user.id,
            image_thumb_b64=_THUMB_B64,
            top_upstream_id="pokemon:base1-4",
            candidates_json=[
                {"name": "Charizard", "confidence": 0.91, "source": "text"},
                {"name": "Charmeleon", "confidence": 0.4, "source": "text"},
            ],
        )
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner/history", headers=admin_headers)
    )

    assert body["total"] == 1
    item = body["items"][0]
    assert item["user_id"] == str(created_user.id)
    assert item["user_email"] == created_user.email
    assert item["image_url"].startswith("data:image/jpeg;base64,")
    assert item["top_name"] == "Charizard"
    assert item["candidate_count"] == 2


@pytest.mark.asyncio
async def test_history_keeps_anonymous_scans(client, admin_headers, db_session):
    """The camera-first flow lets people scan before signing up, so an
    unattributed row is normal data — not something to hide or crash on."""
    db_session.add(_scan(user_id=None))
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner/history", headers=admin_headers)
    )
    assert body["total"] == 1
    assert body["items"][0]["user_id"] is None
    assert body["items"][0]["user_email"] is None


@pytest.mark.asyncio
async def test_history_is_newest_first(client, admin_headers, db_session):
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _scan(parsed_title="older", created_at=now - timedelta(hours=2)),
            _scan(parsed_title="newer", created_at=now),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/admin/scanner/history", headers=admin_headers)
    )
    assert [i["parsed_title"] for i in body["items"]] == ["newer", "older"]


@pytest.mark.asyncio
async def test_history_pages_with_a_cursor_that_stops_at_the_end(
    client, admin_headers, db_session
):
    """Rows carry thumbnails, so the page size is small and the caller is told
    explicitly when to stop — a null cursor, not an empty page they discover by
    asking for one more."""
    now = datetime.now(UTC)
    db_session.add_all([_scan(created_at=now - timedelta(minutes=i)) for i in range(3)])
    await db_session.commit()

    first = assert_envelope_ok(
        await client.get("/v1/admin/scanner/history?limit=2", headers=admin_headers)
    )
    assert len(first["items"]) == 2
    assert first["total"] == 3
    assert first["next_cursor"] == "2"

    last = assert_envelope_ok(
        await client.get(
            "/v1/admin/scanner/history?limit=2&offset=2", headers=admin_headers
        )
    )
    assert len(last["items"]) == 1
    assert last["next_cursor"] is None


@pytest.mark.asyncio
async def test_history_filters_to_the_misses(client, admin_headers, db_session):
    """``matched=false`` is the debugging view — the scans where we returned
    nothing usable, which is the queue the identify work gets prioritised
    from."""
    db_session.add_all(
        [
            _scan(top_upstream_id="pokemon:base1-4", parsed_title="hit"),
            _scan(top_upstream_id=None, parsed_title="miss"),
        ]
    )
    await db_session.commit()

    misses = assert_envelope_ok(
        await client.get(
            "/v1/admin/scanner/history?matched=false", headers=admin_headers
        )
    )
    assert [i["parsed_title"] for i in misses["items"]] == ["miss"]

    hits = assert_envelope_ok(
        await client.get(
            "/v1/admin/scanner/history?matched=true", headers=admin_headers
        )
    )
    assert [i["parsed_title"] for i in hits["items"]] == ["hit"]


@pytest.mark.asyncio
async def test_history_filters_stack(client, admin_headers, created_user, db_session):
    """The grid's filter bar sends several at once, and they must intersect
    rather than the last one winning."""
    db_session.add_all(
        [
            _scan(
                user_id=created_user.id,
                primary_source="phash",
                top_confidence=0.95,
                parsed_title="wanted",
            ),
            _scan(user_id=created_user.id, primary_source="text", top_confidence=0.95),
            _scan(user_id=None, primary_source="phash", top_confidence=0.95),
            _scan(user_id=created_user.id, primary_source="phash", top_confidence=0.10),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get(
            "/v1/admin/scanner/history"
            f"?user_id={created_user.id}&source=phash&min_confidence=0.5",
            headers=admin_headers,
        )
    )
    assert body["total"] == 1
    assert body["items"][0]["parsed_title"] == "wanted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=101",  # thumbnails make an unbounded page expensive
        "offset=-1",
        "min_confidence=1.5",  # confidence is a 0..1 probability
        "user_id=not-a-uuid",
    ],
)
async def test_history_rejects_impossible_filters(client, admin_headers, query):
    resp = await client.get(f"/v1/admin/scanner/history?{query}", headers=admin_headers)
    assert_envelope_error(resp, expected_status=422)


# ── GET /v1/admin/scanner/history/{scan_id} ─────────────────────────────────


@pytest.mark.asyncio
async def test_scan_detail_adds_the_raw_ocr_and_every_candidate(
    client, admin_headers, db_session
):
    """The drill-down is where a bad match gets diagnosed, so it carries what
    the list view deliberately omits: the raw OCR text and the full ranked
    list, not just the winner."""
    row = _scan(
        ocr_full_text="CHARIZARD 4/102 BASE SET",
        ocr_confidence=0.88,
        parsed_set_code="base1",
        phash="ff00ff00",
        candidates_json=[
            {
                "upstream_id": "pokemon:base1-4",
                "name": "Charizard",
                "confidence": 0.91,
                "source": "text",
            },
            {"name": "Charmeleon", "confidence": 0.42, "source": "text"},
        ],
    )
    db_session.add(row)
    await db_session.flush()
    db_session.add(IdentificationFeedback(identification_id=row.id, correct=False))
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get(f"/v1/admin/scanner/history/{row.id}", headers=admin_headers)
    )

    assert body["ocr_full_text"] == "CHARIZARD 4/102 BASE SET"
    assert body["ocr_confidence"] == 0.88
    assert body["parsed_set_code"] == "base1"
    assert body["phash"] == "ff00ff00"
    assert [c["name"] for c in body["candidates"]] == ["Charizard", "Charmeleon"]
    # The user told us this one was wrong — that verdict rides along.
    assert body["feedback_correct"] is False


@pytest.mark.asyncio
async def test_scan_detail_for_an_unknown_id_is_404(client, admin_headers):
    resp = await client.get(
        f"/v1/admin/scanner/history/{uuid.uuid4()}", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=404)


@pytest.mark.asyncio
async def test_a_malformed_scan_id_is_rejected_before_the_lookup(client, admin_headers):
    resp = await client.get(
        "/v1/admin/scanner/history/not-a-uuid", headers=admin_headers
    )
    assert_envelope_error(resp, expected_status=422)
