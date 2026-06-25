"""Tests for the admin engagement / retention analytics."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.card import Card, CardSet
from app.models.enums import (
    GradeHouseEnum,
    ScanSourceEnum,
    ScanStatusEnum,
    TcgEnum,
)
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.services.admin import engagement_service


@pytest.mark.asyncio
async def test_engagement_summary(db_session, created_user):
    cs = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db_session.add(cs)
    await db_session.flush()
    card = Card(set_id=cs.id, tcg=TcgEnum.pokemon, name="Pikachu")
    db_session.add(card)
    await db_session.flush()

    # The user adds a card (activation) and scans (activity).
    db_session.add_all(
        [
            GradedCard(
                user_id=created_user.id,
                card_id=card.id,
                grade=Decimal("9.0"),
                house=GradeHouseEnum.psa,
                graded_at=datetime.now(UTC),
            ),
            ScanJob(
                user_id=created_user.id,
                status=ScanStatusEnum.complete,
                source=ScanSourceEnum.phone,
            ),
        ]
    )
    await db_session.commit()

    s = await engagement_service.summary(db_session)
    assert s.total_users >= 1
    assert s.activated_users == 1
    assert 0 < s.activation_rate <= 1
    # Recent scan/add → active in every window.
    assert s.active_7d == 1
    assert s.active_30d == 1
    assert s.active_90d == 1
    # Funnel: signed-up >= activated >= pro.
    counts = [f.count for f in s.funnel]
    assert counts == sorted(counts, reverse=True)
    assert [f.label for f in s.funnel] == ["Signed up", "Added a card", "Upgraded to Pro"]
    assert len(s.new_users_by_week) == 8
