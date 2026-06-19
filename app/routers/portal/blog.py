"""Public blog endpoints (`/v1/blog`) — published posts only."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.portal import BlogPostRead
from app.services.portal import blog_service

router = APIRouter(prefix="/blog", tags=["blog"])


@router.get("/posts", response_model=list[BlogPostRead], summary="List published posts")
async def list_posts(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[BlogPostRead]:
    rows = await blog_service.list_published(db, limit=limit, offset=offset)
    return [BlogPostRead.model_validate(r) for r in rows]


@router.get(
    "/posts/{slug}", response_model=BlogPostRead, summary="Get a published post by slug"
)
async def get_post(slug: str, db: AsyncSession = Depends(get_db)) -> BlogPostRead:
    row = await blog_service.get_published_by_slug(db, slug)
    return BlogPostRead.model_validate(row)


__all__ = ["router"]
