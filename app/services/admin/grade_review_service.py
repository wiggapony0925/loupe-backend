"""Grade-review queue — QA surface over graded cards (Loupe's first-party grade
by default). Read-only."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.card import Card, CardSet
from app.models.enums import GradeHouseEnum
from app.models.grade import GradedCard
from app.models.user import User
from app.schemas.grade_review import GradeReviewPage, GradeReviewRow

# Default to first-party Loupe grades — those are the ones that want human QA.
_DEFAULT_HOUSE = GradeHouseEnum.loupe.value


def _enum(value: object) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


async def list_grades(
    db: AsyncSession, *, house: str = _DEFAULT_HOUSE, page: int = 1, page_size: int = 25
) -> GradeReviewPage:
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    base = (
        select(GradedCard, User.email, Card.name, Card.image_url, CardSet.name)
        .join(User, GradedCard.user_id == User.id)
        .join(Card, GradedCard.card_id == Card.id)
        .join(CardSet, Card.set_id == CardSet.id)
        .where(GradedCard.deleted_at.is_(None))
    )
    # "all" shows every house; otherwise filter to a valid house (default loupe).
    if house and house != "all":
        try:
            base = base.where(GradedCard.house == GradeHouseEnum(house))
        except ValueError:
            base = base.where(GradedCard.house == GradeHouseEnum.loupe)

    total = await db.scalar(
        select(func.count()).select_from(base.order_by(None).subquery())
    )
    rows = (
        await db.execute(
            base.order_by(GradedCard.graded_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    houses = (
        (
            await db.execute(
                select(GradedCard.house)
                .where(GradedCard.deleted_at.is_(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    results = [
        GradeReviewRow(
            id=g.id,
            user_email=email,
            card_name=card_name,
            card_image_url=image_url,
            set_name=set_name,
            house=_enum(g.house) or "",
            grade=float(g.grade),
            subgrades=g.subgrades,
            condition=_enum(g.condition),
            estimated_value_usd=float(g.estimated_value_usd)
            if g.estimated_value_usd is not None
            else None,
            acquired_via=_enum(g.acquired_via),
            graded_at=g.graded_at,
        )
        for g, email, card_name, image_url, set_name in rows
    ]
    return GradeReviewPage(
        results=results,
        total=int(total or 0),
        page=page,
        page_size=page_size,
        houses=sorted({_enum(h) or "" for h in houses}),
    )


__all__ = ["list_grades"]
