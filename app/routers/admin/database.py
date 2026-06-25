"""Admin database explorer (`/v1/admin/database`) — read-only introspection."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.ops import DatabaseOverview, SchemaGraph, TableDetail
from app.services.admin import schema_service

router = APIRouter(prefix="/database", tags=["admin-ops"])


@router.get(
    "/tables",
    response_model=DatabaseOverview,
    summary="All tables with column/FK counts and row estimates",
)
async def list_tables(db: AsyncSession = Depends(get_db)) -> DatabaseOverview:
    return await schema_service.overview(db)


@router.get(
    "/graph",
    response_model=SchemaGraph,
    summary="Foreign-key relationship graph (nodes + edges)",
)
async def get_graph() -> SchemaGraph:
    return schema_service.graph()


@router.get(
    "/tables/{name}",
    response_model=TableDetail,
    summary="Columns, indexes, and relationships for one table",
)
async def get_table(name: str, db: AsyncSession = Depends(get_db)) -> TableDetail:
    detail = await schema_service.table_detail(db, name)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown table")
    return detail


__all__ = ["router"]
