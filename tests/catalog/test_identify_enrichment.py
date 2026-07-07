"""Identify enrichment — server-composed pricing + ownership per candidate.

Proves a scan candidate comes back knowing (a) its market price from the
catalog mirror, (b) whether the signed-in user owns it, how many copies,
and how many are graded slabs — including when the candidate only carries
an upstream id and ownership hangs off the materialized local card.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.card_external_ref import CardExternalRef
from app.models.catalog_mirror import CatalogMirrorCard
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.identification import IdentifyCandidate
from app.services.catalog.identify_enrichment_service import enrich_candidates
from tests.factories import make_card


def _candidate(**kw) -> IdentifyCandidate:
    base = dict(name="Charizard", confidence=0.9, source="phash")
    base.update(kw)
    return IdentifyCandidate(**base)


async def _mk_user(db) -> User:  # noqa: ANN001
    user = User(
        id=uuid.uuid4(),
        email=f"enrich-{uuid.uuid4().hex[:8]}@test.dev",
        display_name="Enrich",
    )
    db.add(user)
    await db.commit()
    return user


async def _mk_mirror_row(db, upstream_id: str, price: float | None) -> None:  # noqa: ANN001
    source, external = upstream_id.split(":", 1)
    db.add(
        CatalogMirrorCard(
            id=upstream_id,
            source=source,
            tcg="pokemon",
            upstream_id=external,
            set_id="base1",
            name="Charizard",
            name_lower="charizard",
            sort_price=price,
            payload={},
        )
    )
    await db.commit()


@pytest.mark.anyio
async def test_price_comes_from_mirror(db_session):  # noqa: ANN001
    await _mk_mirror_row(db_session, "pokemontcg:base1-4", 412.5)
    cand = _candidate(upstream_id="pokemontcg:base1-4")

    await enrich_candidates(db_session, None, [cand])

    assert cand.market_price_usd == 412.5
    assert cand.owned is False and cand.copies_owned == 0


@pytest.mark.anyio
async def test_ownership_via_local_card_id(db_session):  # noqa: ANN001
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Charizard")
    for house in (GradeHouseEnum.psa, GradeHouseEnum.loupe, GradeHouseEnum.loupe):
        db_session.add(
            GradedCard(
                user_id=user.id,
                card_id=card.id,
                grade=Decimal("9"),
                house=house,
                estimated_value_usd=Decimal("100"),
            )
        )
    await db_session.commit()

    cand = _candidate(card_id=str(card.id))
    await enrich_candidates(db_session, user, [cand])

    assert cand.owned is True
    assert cand.copies_owned == 3
    assert cand.graded_copies == 1  # only the PSA slab counts as graded


@pytest.mark.anyio
async def test_ownership_via_upstream_external_ref(db_session):  # noqa: ANN001
    """Candidate carries only an upstream id; ownership resolves through the
    external-ref mapping to the materialized local card."""
    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Blastoise")
    db_session.add(
        CardExternalRef(card_id=card.id, source="pokemontcg", external_id="base1-2")
    )
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("8"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("80"),
        )
    )
    await db_session.commit()
    await _mk_mirror_row(db_session, "pokemontcg:base1-2", 99.0)

    cand = _candidate(upstream_id="pokemontcg:base1-2", name="Blastoise")
    await enrich_candidates(db_session, user, [cand])

    assert cand.market_price_usd == 99.0
    assert cand.owned is True
    assert cand.copies_owned == 1
    assert cand.graded_copies == 1


@pytest.mark.anyio
async def test_guest_gets_pricing_but_no_ownership(db_session):  # noqa: ANN001
    await _mk_mirror_row(db_session, "pokemontcg:base1-9", 55.0)
    cand = _candidate(upstream_id="pokemontcg:base1-9")

    await enrich_candidates(db_session, None, [cand])

    assert cand.market_price_usd == 55.0
    assert cand.owned is False


@pytest.mark.anyio
async def test_soft_deleted_holdings_do_not_count(db_session):  # noqa: ANN001
    from datetime import UTC, datetime

    user = await _mk_user(db_session)
    card = await make_card(db_session, name="Venusaur")
    db_session.add(
        GradedCard(
            user_id=user.id,
            card_id=card.id,
            grade=Decimal("9"),
            house=GradeHouseEnum.psa,
            estimated_value_usd=Decimal("60"),
            deleted_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    cand = _candidate(card_id=str(card.id), name="Venusaur")
    await enrich_candidates(db_session, user, [cand])

    assert cand.owned is False
    assert cand.copies_owned == 0
