"""One-click unsubscribe for announcement email (`/v1/public/unsubscribe`).

Unauthenticated by design — the recipient may be signed out (or reading on a
different device) and mail providers hit the POST form themselves for
RFC 8058 one-click (``List-Unsubscribe-Post``). Authorization comes from the
HMAC-signed token minted per recipient at send time; it can only ever turn
announcement email *off*, so replay is harmless. GET renders a tiny branded
confirmation page for humans who click the footer link.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.services.auth import unsubscribe_service

router = APIRouter(prefix="/public/unsubscribe", tags=["public"])


def _page(title: str, message: str) -> str:
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
        f'<a href="{app_url}/app/settings" style="color:#00C896;'
        'font-weight:600;text-decoration:none;">Manage email preferences &rarr;</a>'
        "</div></body></html>"
    )


async def _handle(token: str, db: AsyncSession) -> HTMLResponse:
    user_id = unsubscribe_service.resolve_token(token)
    ok = (
        await unsubscribe_service.apply_unsubscribe(db, user_id)
        if user_id is not None
        else False
    )
    if not ok:
        return HTMLResponse(
            _page(
                "That link didn't work",
                "This unsubscribe link is invalid or no longer matches an "
                "account. You can manage email preferences from Settings "
                "after signing in.",
            ),
            status_code=400,
        )
    return HTMLResponse(
        _page(
            "You're unsubscribed",
            "You won't receive product updates or blog announcements from "
            "Loupe anymore. Security and account emails still arrive — "
            "they're how we reach you if something's wrong.",
        )
    )


@router.get("", response_class=HTMLResponse, summary="Unsubscribe (footer link)")
async def unsubscribe_get(
    token: str = Query(min_length=10, max_length=200),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    return await _handle(token, db)


@router.post(
    "", response_class=HTMLResponse, summary="One-click unsubscribe (RFC 8058)"
)
async def unsubscribe_post(
    token: str = Query(min_length=10, max_length=200),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    return await _handle(token, db)


__all__ = ["router"]
