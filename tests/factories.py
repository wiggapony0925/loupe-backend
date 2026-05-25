"""Factories for building ORM rows in tests."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.enums import TcgEnum
from app.models.scanner import Scanner
from app.models.user import User, UserSettings


async def make_user(db: AsyncSession, *, email: str | None = None) -> User:
    user = User(
        email=email or f"u+{uuid.uuid4().hex[:8]}@example.com",
        display_name="Factory User",
        apple_subject=f"apple-{uuid.uuid4().hex}",
    )
    db.add(user)
    await db.flush()
    db.add(UserSettings(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def make_card_set(db: AsyncSession, *, tcg: TcgEnum = TcgEnum.pokemon) -> CardSet:
    cset = CardSet(tcg=tcg, name=f"Set {uuid.uuid4().hex[:6]}", code="TST")
    db.add(cset)
    await db.commit()
    await db.refresh(cset)
    return cset


async def make_card(
    db: AsyncSession,
    *,
    set_id: uuid.UUID | None = None,
    name: str = "Test Card",
    year: int | None = None,
) -> Card:
    if set_id is None:
        cset = await make_card_set(db)
        set_id = cset.id
    card = Card(set_id=set_id, tcg=TcgEnum.pokemon, name=name, year=year)
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


async def make_card_with_price_history(
    db: AsyncSession,
    history: list[tuple[date, float]],
    *,
    set_id: uuid.UUID | None = None,
    name: str = "Test Card",
) -> Card:
    """Create a card whose `card_metadata['price_history']` is pre-populated.

    `history` is a list of `(date, priceUsd)` pairs that will be stored in
    the canonical list-of-dicts shape the portfolio service consumes.
    """
    if set_id is None:
        cset = await make_card_set(db)
        set_id = cset.id
    card = Card(
        set_id=set_id,
        tcg=TcgEnum.pokemon,
        name=name,
        card_metadata={
            "price_history": [
                {"date": d.isoformat(), "priceUsd": float(p)} for d, p in history
            ],
        },
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


async def make_scanner(db: AsyncSession, user: User) -> Scanner:
    sc = Scanner(
        owner_id=user.id, device_id=f"dev-{uuid.uuid4().hex[:8]}", name="My Scanner"
    )
    db.add(sc)
    await db.commit()
    await db.refresh(sc)
    return sc
