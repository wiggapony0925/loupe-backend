"""Public Loupe Scanner waitlist endpoints (`/v1/waitlist`).

Join the waitlist (the scanner product page's "checkout" CTA) and read
aggregate stats for social proof. No auth required, but a signed-in
visitor is linked to their signup so the admin portal can see which real
accounts are in line.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import optional_user
from app.db import get_db
from app.models.user import User
from app.platform.rate_limit import waitlist_join_limit
from app.schemas.waitlist import WaitlistJoin, WaitlistJoined, WaitlistStats
from app.services.portal import waitlist_service

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


@router.post(
    "",
    response_model=WaitlistJoined,
    status_code=201,
    summary="Join the Loupe Scanner waitlist",
    dependencies=[Depends(waitlist_join_limit)],
)
async def join(
    payload: WaitlistJoin,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> WaitlistJoined:
    return await waitlist_service.join(
        db, payload, user_id=user.id if user is not None else None
    )


@router.get(
    "/stats",
    response_model=WaitlistStats,
    summary="Aggregate waitlist counts (for social proof)",
)
async def stats(db: AsyncSession = Depends(get_db)) -> WaitlistStats:
    return await waitlist_service.stats(db)


__all__ = ["router"]
