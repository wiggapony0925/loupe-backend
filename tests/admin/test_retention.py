"""Tests for the admin cohort-retention triangle."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, ScanSourceEnum, ScanStatusEnum, TcgEnum
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.services.admin import retention_service


@pytest.mark.asyncio
async def test_retention_triangle(db_session, created_user):
    cs = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db_session.add(cs)
    await db_session.flush()
    card = Card(set_id=cs.id, tcg=TcgEnum.pokemon, name="Blastoise")
    db_session.add(card)
    await db_session.flush()

    # The (freshly signed-up) user is active in week 0 — scan + card add.
    db_session.add_all(
        [
            ScanJob(
                user_id=created_user.id,
                status=ScanStatusEnum.complete,
                source=ScanSourceEnum.phone,
            ),
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.psa,
                graded_at=datetime.now(UTC),
            ),
        ]
    )
    await db_session.commit()

    report = await retention_service.cohorts(db_session, weeks=8)

    assert report.weeks == 8
    assert len(report.cohorts) == 8
    # Cohorts run oldest → newest; the triangle narrows by one week per row.
    assert [len(c.retention) for c in report.cohorts] == [8, 7, 6, 5, 4, 3, 2, 1]
    # Every value is a valid share.
    assert all(0.0 <= v <= 1.0 for c in report.cohorts for v in c.retention)

    # Exactly one user, in the newest cohort, active in its signup week.
    assert sum(c.size for c in report.cohorts) == 1
    newest = report.cohorts[-1]
    assert newest.size == 1
    assert newest.retention == [1.0]


@pytest.mark.asyncio
async def test_retention_empty(db_session):
    report = await retention_service.cohorts(db_session, weeks=4)
    assert report.weeks == 4
    assert len(report.cohorts) == 4
    assert all(c.size == 0 for c in report.cohorts)
    assert all(v == 0.0 for c in report.cohorts for v in c.retention)
