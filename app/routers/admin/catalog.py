"""Admin catalog-coverage analytics (`/v1/admin/catalog`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.catalog import CatalogCoverage
from app.services.admin import catalog_service

router = APIRouter(prefix="/catalog", tags=["admin-catalog"])


@router.get("", response_model=CatalogCoverage, summary="Catalog coverage by game")
async def get_catalog(db: AsyncSession = Depends(get_db)) -> CatalogCoverage:
    return await catalog_service.summary(db)


__all__ = ["router"]
