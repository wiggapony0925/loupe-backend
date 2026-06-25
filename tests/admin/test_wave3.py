"""Wave 3 tests — catalog coverage + scanner funnel analytics."""

from __future__ import annotations

import uuid

import pytest

from app.models.card import Card, CardSet
from app.models.enums import ScanSourceEnum, ScanStatusEnum, TcgEnum
from app.models.identification import CardIdentification, IdentificationFeedback
from app.models.scan import ScanJob
from app.services.admin import catalog_service, scanner_stats_service


# ── catalog_service ──
@pytest.mark.asyncio
async def test_catalog_coverage_counts_and_phash(db_session):
    card_set = CardSet(tcg=TcgEnum.pokemon, name="Base Set")
    db_session.add(card_set)
    await db_session.flush()
    db_session.add_all(
        [
            Card(set_id=card_set.id, tcg=TcgEnum.pokemon, name="Pikachu", image_phash="abc"),
            Card(set_id=card_set.id, tcg=TcgEnum.pokemon, name="Charizard"),  # no phash
        ]
    )
    await db_session.commit()

    cov = await catalog_service.summary(db_session)
    assert cov.total_cards == 2
    assert cov.total_sets == 1
    assert cov.phash_coverage_pct == 0.5
    # One row per game in the enum.
    assert len(cov.games) == len(list(TcgEnum))

    pokemon = next(g for g in cov.games if g.tcg == "pokemon")
    assert pokemon.cards == 2 and pokemon.phash_cards == 1
    assert pokemon.phash_pct == 0.5 and pokemon.backed is True

    # A game with no catalog data is flagged as scaffolded (not backed).
    empty = next(g for g in cov.games if g.cards == 0)
    assert empty.backed is False


# ── scanner_stats_service ──
@pytest.mark.asyncio
async def test_scanner_funnel_metrics(db_session, created_user):
    ids = [
        CardIdentification(
            image_sha256=uuid.uuid4().hex,
            ocr_provider="mock",
            tcg_inferred="pokemon",
            primary_source="phash",
            top_confidence=0.9,
        ),
        CardIdentification(
            image_sha256=uuid.uuid4().hex,
            ocr_provider="mock",
            tcg_inferred="pokemon",
            primary_source="text",
            top_confidence=0.7,
        ),
    ]
    db_session.add_all(ids)
    await db_session.flush()
    db_session.add(
        IdentificationFeedback(identification_id=ids[0].id, correct=True)
    )
    db_session.add(
        ScanJob(
            user_id=created_user.id,
            status=ScanStatusEnum.complete,
            source=ScanSourceEnum.phone,
        )
    )
    await db_session.commit()

    s = await scanner_stats_service.summary(db_session, days=30)
    assert s.total_identifications == 2
    assert s.by_source == {"phash": 1, "text": 1}
    assert s.fast_path_rate == 0.5
    assert s.total_feedback == 1
    assert s.correct_feedback == 1
    assert s.top1_accuracy == 1.0
    assert s.scans_total == 1
    assert s.scans_by_status == {"complete": 1}
