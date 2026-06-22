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

from fastapi import APIRouter, Depends, Request
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
    except Exception:
        # Returning 200 stops Stripe's retries from hammering us on a bug we
        # need to fix forward; the error is logged for follow-up.
        logger.exception("billing webhook handler failed for %s", event.get("type"))
    return {"received": True}


__all__ = ["router"]
