"""Admin grade-review queue (`/v1/admin/grades`) — read-only QA of grades."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.grade_review import GradeReviewPage
from app.services.admin import grade_review_service

router = APIRouter(prefix="/grades", tags=["admin-grades"])


@router.get("", response_model=GradeReviewPage, summary="Grade-review queue")
async def list_grades(
    house: str = Query("loupe", description="A grading house, or 'all'."),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> GradeReviewPage:
    return await grade_review_service.list_grades(
        db, house=house, page=page, page_size=page_size
    )


__all__ = ["router"]
