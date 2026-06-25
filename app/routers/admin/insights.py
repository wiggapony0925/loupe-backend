"""Admin "Ask your data" — natural-language SQL queries (`/v1/admin/insights`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_super_admin
from app.db import get_db
from app.models.user import User
from app.schemas.insights import AskRequest, AskResponse
from app.services import audit_service
from app.services.admin import nl_query_service

router = APIRouter(prefix="/insights", tags=["admin-insights"])


@router.get("/status", summary="Whether natural-language queries are configured")
async def insights_status() -> dict[str, bool]:
    return {"configured": nl_query_service.configured()}


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask a question of the database (read-only, super-admin)",
)
async def ask(
    payload: AskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_super_admin),
) -> AskResponse:
    result = await nl_query_service.ask(payload.question)
    await audit_service.record(
        db,
        request=request,
        user=actor,
        action="insights.ask",
        target_table=None,
        payload={"question": payload.question, "sql": result.sql},
    )
    return result


__all__ = ["router"]
