"""Tests for the admin live activity feed (pulse)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.audit import AuditLog
from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum, ScanSourceEnum, ScanStatusEnum, TcgEnum
from app.models.grade import GradedCard
from app.models.scan import ScanJob
from app.services.admin import pulse_service


@pytest.mark.asyncio
async def test_pulse_merges_all_sources(db_session, created_user):
    cs = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db_session.add(cs)
    await db_session.flush()
    card = Card(set_id=cs.id, tcg=TcgEnum.pokemon, name="Charizard")
    db_session.add(card)
    await db_session.flush()

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
                estimated_value_usd=Decimal("1200.00"),
                graded_at=datetime.now(UTC),
            ),
            AuditLog(user_id=created_user.id, action="flag.update", target_table="feature_flags"),
        ]
    )
    await db_session.commit()

    feed = await pulse_service.recent(db_session, limit=40)
    types = {e.type for e in feed.events}
    assert {"signup", "scan", "acquisition", "admin"} <= types

    # Newest-first ordering.
    ats = [e.at for e in feed.events]
    assert ats == sorted(ats, reverse=True)

    acquisition = next(e for e in feed.events if e.type == "acquisition")
    assert acquisition.title == "Added Charizard"
    assert acquisition.detail == "PSA 9.0"
    assert acquisition.value_usd == 1200.0


@pytest.mark.asyncio
async def test_pulse_respects_limit(db_session, created_user):
    feed = await pulse_service.recent(db_session, limit=1)
    assert len(feed.events) <= 1
