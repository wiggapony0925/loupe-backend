"""Email verification landing (`/v1/public/verify-email`).

Unauthenticated: the recipient may open the link on a device where they're
signed out. The signed token scopes the action to one account and can only
flip verification ON, so replay is harmless. Renders a tiny branded page
(mirrors the unsubscribe page).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.services.auth import email_verify_service

router = APIRouter(prefix="/public/verify-email", tags=["public"])


def _page(title: str, message: str, cta_label: str) -> str:
    app_url = get_settings().app_public_url.rstrip("/")
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title} — Loupe</title></head>"
        '<body style="margin:0;padding:48px 16px;background:#F4F5F7;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        'Helvetica,Arial,sans-serif;color:#0E1117;">'
        '<div style="max-width:480px;margin:0 auto;background:#fff;'
        'border:1px solid #E5E8EC;border-radius:18px;padding:32px;">'
        '<div style="color:#00C896;font-weight:800;font-size:17px;'
        'margin-bottom:16px;">&#9670; LOUPE</div>'
        f'<h1 style="margin:0 0 10px;font-size:22px;">{title}</h1>'
        f'<p style="margin:0 0 20px;line-height:1.6;color:#5B6470;">{message}</p>'
        f'<a href="{app_url}/app" style="color:#00C896;'
        f'font-weight:600;text-decoration:none;">{cta_label} &rarr;</a>'
        "</div></body></html>"
    )


@router.get("", response_class=HTMLResponse, summary="Confirm an email address")
async def verify_email(
    token: str = Query(min_length=10, max_length=200),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    user_id = email_verify_service.resolve_token(token)
    ok = (
        await email_verify_service.apply_verification(db, user_id)
        if user_id is not None
        else False
    )
    if not ok:
        return HTMLResponse(
            _page(
                "That link didn't work",
                "This verification link is invalid or no longer matches an "
                "account. You can request a fresh one from Settings after "
                "signing in.",
                "Open Loupe",
            ),
            status_code=400,
        )
    return HTMLResponse(
        _page(
            "Email confirmed",
            "Thanks — your email address is verified. Alerts, statements, "
            "and account notices will reach you reliably.",
            "Back to your vault",
        )
    )


__all__ = ["router"]
