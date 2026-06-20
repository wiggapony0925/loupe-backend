"""Price-alert CRUD + worker evaluation.

* `create/list/delete` are user-scoped REST helpers wired into the
  router below.
* `evaluate_for_card` is what the price-backfill worker calls after it
  lands a new latest price. It atomically flips every still-pending
  alert that the new price satisfies and returns the rows that just
  fired so the caller can fan out push notifications.

All math uses Decimal to match the column type, so we don't lose
precision converting through floats.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card
from app.models.enums import PriceAlertCondition
from app.models.price_alert import PriceAlert
from app.models.user import User
from app.schemas.price_alert import PriceAlertCreate, PriceAlertRead


def _to_read(row: PriceAlert, card: Card | None) -> PriceAlertRead:
    out = PriceAlertRead.model_validate(row)
    if card is not None:
        out.card_name = card.name
        out.card_image_url = card.image_url
    return out


async def list_for_user(
    db: AsyncSession, user: User, *, include_triggered: bool = True
) -> list[PriceAlertRead]:
    stmt = (
        select(PriceAlert, Card)
        .outerjoin(Card, Card.id == PriceAlert.card_id)
        .where(PriceAlert.user_id == user.id)
        .order_by(PriceAlert.created_at.desc())
    )
    if not include_triggered:
        stmt = stmt.where(PriceAlert.triggered_at.is_(None))
    rows = (await db.execute(stmt)).all()
    return [_to_read(a, c) for (a, c) in rows]


class CardNotResolvable(Exception):
    """Raised when an alert's ``upstream_id`` can't be materialized locally."""


async def create(
    db: AsyncSession, user: User, payload: PriceAlertCreate
) -> PriceAlertRead:
    # Web sends an upstream ``<source>:<id>`` rather than a local UUID, so
    # materialize a local Card for it first (idempotent — repeat alerts on the
    # same card reuse the row). Mobile keeps passing a resolved ``card_id``.
    card_id = payload.card_id
    card: Card | None = None
    if card_id is None:
        from app.services.catalog import card_resolver_service

        card = await card_resolver_service.ensure_local_card(
            db, upstream_id=payload.upstream_id or ""
        )
        if card is None:
            raise CardNotResolvable(payload.upstream_id or "")
        card_id = card.id

    row = PriceAlert(
        user_id=user.id,
        card_id=card_id,
        condition=payload.condition,
        threshold_usd=payload.threshold_usd,
        note=payload.note,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    if card is None:
        card = (
            await db.execute(select(Card).where(Card.id == card_id))
        ).scalar_one_or_none()
    return _to_read(row, card)


async def delete(db: AsyncSession, user: User, alert_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(PriceAlert).where(
                PriceAlert.id == alert_id, PriceAlert.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


def _satisfies(
    condition: PriceAlertCondition, price: Decimal, threshold: Decimal
) -> bool:
    if condition == PriceAlertCondition.above:
        return price >= threshold
    if condition == PriceAlertCondition.below:
        return price <= threshold
    return False


async def evaluate_for_card(
    db: AsyncSession, card_id: uuid.UUID, new_price_usd: Decimal
) -> list[PriceAlert]:
    """Fire any pending alerts for `card_id` that `new_price_usd` satisfies.

    Returns the rows that just transitioned to triggered so the caller
    can fan out push notifications. Does not commit — the caller owns
    the transaction so this is composable with the worker's batch.
    """
    pending = (
        (
            await db.execute(
                select(PriceAlert).where(
                    PriceAlert.card_id == card_id,
                    PriceAlert.triggered_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    fired: list[PriceAlert] = []
    for alert in pending:
        if _satisfies(alert.condition, new_price_usd, alert.threshold_usd):
            alert.triggered_at = now
            alert.triggered_price_usd = new_price_usd
            fired.append(alert)
    return fired


def serialize_for_wire(alert: PriceAlert, card: Card | None = None) -> dict[str, Any]:
    """Mirror the `PriceAlertRead` shape without a Pydantic round-trip."""
    return _to_read(alert, card).model_dump(mode="json")


__all__ = [
    "CardNotResolvable",
    "create",
    "delete",
    "evaluate_for_card",
    "list_for_user",
    "serialize_for_wire",
]
