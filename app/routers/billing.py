"""Stripe billing webhook (`/v1/billing/webhook`).

Public + unauthenticated — Stripe calls it server-to-server — but every payload
is signature-verified against ``STRIPE_WEBHOOK_SECRET`` before we trust a byte
of it. This is the *only* path that grants a paid plan: it relays Stripe's
subscription lifecycle into ``user.plan`` / ``pro_expires_at``.

Local testing:

    stripe listen --forward-to localhost:8000/v1/billing/webhook
    # copy the printed whsec_... into STRIPE_WEBHOOK_SECRET, then:
    stripe trigger checkout.session.completed
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services import billing_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


@router.post("/webhook", summary="Stripe subscription lifecycle webhook")
async def stripe_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    # Signature verification needs the exact raw bytes, not a re-serialized dict.
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    event = billing_service.construct_event(payload, signature)
    try:
        await billing_service.handle_event(db, event)
    except HTTPException:
        # A deliberate refusal — most often a half-configured deploy, where
        # STRIPE_SECRET_KEY is missing so handle_event 503s on its first line.
        # The event was *not* processed, so it must not be acknowledged.
        logger.exception("billing webhook refused %s", event.get("type"))
        raise
    except Exception as exc:
        # Anything unexpected (DB blip, lock timeout, deadlock) is transient
        # until proven otherwise. A 200 here tells Stripe never to redeliver,
        # which turns a momentary failure into a permanently lost subscription
        # event; a 5xx puts it back on Stripe's retry schedule instead. Genuine
        # no-ops — unknown customer, event type we don't subscribe to — return
        # normally and still get their 200.
        logger.exception("billing webhook handler failed for %s", event.get("type"))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook handling failed; please retry.",
        ) from exc
    return {"received": True}


__all__ = ["router"]
