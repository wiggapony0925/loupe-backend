"""Multi-copy ownership contract.

User question: "what if they own more than one of the same card? what if
they graded the same card twice?"

This file pins down the answer:

1. Owning multiple physical copies of the SAME card is first-class:
   each copy is its own ``GradedCard`` row with its own grade, cost
   basis, cert, photos.
2. Portfolio value sums across duplicates (3 Charizards = 3× value).
3. Set-completion counts DISTINCT card_ids (3 Charizards = still 1
   toward "Base Set 102/102").
4. The vault list returns ``copies_owned`` on each row so the UI can
   show an "x3" badge without an extra round-trip.
5. The scan pipeline dedupes identical re-uploads within 5 minutes so
   double-tapping "scan" or arq retrying a job does not create
   phantom rows. Genuine duplicate copies (different scan jobs,
   manual creates, scans more than 5 minutes apart) still produce
   separate rows.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.enums import GradeHouseEnum, ScanStatusEnum
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.services.catalog.card_fingerprint_service import FingerprintResult
from app.tasks import scan_processor
from app.utils.time import utcnow
from tests.conftest import assert_envelope_ok
from tests.factories import make_card

# ----------------------------------------------------------------- vault list


@pytest.mark.asyncio
async def test_vault_list_returns_copies_owned(
    client, auth_headers, db_session, created_user
):
    card = await make_card(db_session)
    other = await make_card(db_session)
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.5"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("250.00"),
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("10.0"),
                house=GradeHouseEnum.psa,
                estimated_value_usd=Decimal("800.00"),
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=other.id,
                grade=Decimal("8.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("40.00"),
            ),
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert len(body) == 4
    by_card: dict[str, list[int]] = {}
    for row in body:
        by_card.setdefault(row["card_id"], []).append(row["copies_owned"])
    # All three Charizard rows report copies_owned=3; the other card=1.
    assert by_card[str(card.id)] == [3, 3, 3]
    assert by_card[str(other.id)] == [1]


@pytest.mark.asyncio
async def test_summary_sums_value_across_duplicate_copies(
    client, auth_headers, db_session, created_user
):
    r"""Three copies × \$100 each must total \$300, not \$100."""
    card = await make_card(db_session)
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
            )
            for _ in range(3)
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/grades/summary", headers=auth_headers)
    )
    assert body["cardCount"] == 3
    assert float(body["totalValueUsd"]) == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_set_progress_counts_distinct_cards_only(
    client, auth_headers, db_session, created_user
):
    """Owning 3 Charizards still counts as 1 toward Base Set completion."""
    card = await make_card(db_session)
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.loupe,
                estimated_value_usd=Decimal("100.00"),
            )
            for _ in range(3)
        ]
    )
    await db_session.commit()

    body = assert_envelope_ok(
        await client.get("/v1/sets/progress", headers=auth_headers)
    )
    assert isinstance(body, list) and len(body) >= 1
    target = next(row for row in body if row["setId"] == str(card.set_id))
    assert target["owned"] == 1  # distinct card count, not row count


# ------------------------------------------------------------ manual re-grade


@pytest.mark.asyncio
async def test_manual_create_allows_a_second_copy(
    client, auth_headers, db_session, created_user
):
    """POST /v1/grades twice with the same card_id creates TWO rows."""
    card = await make_card(db_session)
    body = {
        "card_id": str(card.id),
        "grade": "9.0",
        "house": "loupe",
        "estimated_value_usd": "100.00",
    }
    r1 = await client.post("/v1/grades", headers=auth_headers, json=body)
    r2 = await client.post("/v1/grades", headers=auth_headers, json=body)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["data"]["id"] != r2.json()["data"]["id"]
    # Both rows report copies_owned=2 on subsequent list.
    listing = assert_envelope_ok(await client.get("/v1/grades", headers=auth_headers))
    assert all(row["copies_owned"] == 2 for row in listing)


# ------------------------------------------------------------ scan dedup


@pytest.mark.asyncio
async def test_scan_dedup_within_window(db_session, created_user):
    """Re-running scan_processor for two jobs with the same phash within
    5 minutes must NOT create a second GradedCard."""
    card = await make_card(db_session)

    job_a = ScanJob(
        user_id=created_user.id,
        status=ScanStatusEnum.uploading,
        images_s3_keys={"front": "a/front.jpg"},
    )
    job_b = ScanJob(
        user_id=created_user.id,
        status=ScanStatusEnum.uploading,
        images_s3_keys={"front": "a/front.jpg"},  # same image keys → same phash
    )
    db_session.add_all([job_a, job_b])
    await db_session.commit()

    fixed_fp = FingerprintResult(
        phash="deadbeefcafebabe", dhash="0000000000000000", feature_vector=[0.0] * 16
    )

    class _Sub:
        def as_dict(self) -> dict:
            return {"centering": 9.0, "corners": 9.0, "edges": 9.0, "surface": 9.0}

    class _Grading:
        overall = Decimal("9.0")
        subgrades = _Sub()
        identified_name = None

    # Force the resolver to land on our `card` (no real catalog hit).
    async def _fake_resolve(*_args, **_kwargs):
        from app.services.catalog.card_resolver_service import ResolvedCard

        return ResolvedCard(
            card_id=card.id,
            upstream_id=None,
            unified=None,
            source="phash",
            confidence=0.99,
        )

    with (
        patch.object(scan_processor, "fingerprint_from_images", return_value=fixed_fp),
        patch.object(
            scan_processor, "grade_from_images", new=MagicMock(return_value=_Grading())
        ),
        patch.object(
            scan_processor.card_resolver_service,
            "resolve",
            new=AsyncMock(side_effect=_fake_resolve),
        ),
        patch.object(scan_processor, "_publish", new=AsyncMock(return_value=None)),
    ):
        await scan_processor._process(db_session, job_a.id, created_user.id)
        await scan_processor._process(db_session, job_b.id, created_user.id)

    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(GradedCard).where(
                    GradedCard.user_id == created_user.id,
                    GradedCard.card_id == card.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"expected dedup to 1 row, got {len(rows)}"


@pytest.mark.asyncio
async def test_scan_dedup_does_not_apply_after_window(db_session, created_user):
    """Older than 5 minutes → separate row (user genuinely owns another copy)."""
    card = await make_card(db_session)

    # Seed an existing graded card from "10 minutes ago" with the same phash.
    old = GradedCard(
        user_id=created_user.id,
        card_id=card.id,
        grade=Decimal("9.0"),
        house=GradeHouseEnum.loupe,
        fingerprint_hash="aaaabbbbccccdddd",
    )
    db_session.add(old)
    await db_session.flush()
    old.created_at = utcnow() - timedelta(minutes=10)
    await db_session.commit()

    job = ScanJob(
        user_id=created_user.id,
        status=ScanStatusEnum.uploading,
        images_s3_keys={"front": "b/front.jpg"},
    )
    db_session.add(job)
    await db_session.commit()

    fixed_fp = FingerprintResult(
        phash="aaaabbbbccccdddd", dhash="1111", feature_vector=[0.0] * 16
    )

    class _Sub:
        def as_dict(self) -> dict:
            return {"centering": 9.0, "corners": 9.0, "edges": 9.0, "surface": 9.0}

    class _Grading:
        overall = Decimal("9.5")
        subgrades = _Sub()
        identified_name = None

    async def _fake_resolve(*_args, **_kwargs):
        from app.services.catalog.card_resolver_service import ResolvedCard

        return ResolvedCard(
            card_id=card.id,
            upstream_id=None,
            unified=None,
            source="phash",
            confidence=0.99,
        )

    with (
        patch.object(scan_processor, "fingerprint_from_images", return_value=fixed_fp),
        patch.object(
            scan_processor, "grade_from_images", new=MagicMock(return_value=_Grading())
        ),
        patch.object(
            scan_processor.card_resolver_service,
            "resolve",
            new=AsyncMock(side_effect=_fake_resolve),
        ),
        patch.object(scan_processor, "_publish", new=AsyncMock(return_value=None)),
    ):
        await scan_processor._process(db_session, job.id, created_user.id)

    from sqlalchemy import select

    rows = (
        (
            await db_session.execute(
                select(GradedCard).where(
                    GradedCard.user_id == created_user.id,
                    GradedCard.card_id == card.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2, f"expected two rows after 10m gap, got {len(rows)}"
