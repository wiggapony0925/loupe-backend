"""Resend delivery webhook (`/v1/webhooks/resend`).

Public + unauthenticated — Resend calls it server-to-server — but every
payload is verified against the Svix signature scheme Resend uses
(``RESEND_WEBHOOK_SECRET``, shown when creating the webhook in the Resend
dashboard) before we trust a byte of it.

Events advance the email delivery log by provider message id:
``email.sent → sent``, ``email.delivered → delivered``,
``email.bounced → bounced``, ``email.complained → complained``. Hard bounces
and complaints also auto-suppress announcement mail to that address — the
list-hygiene half of deliverability.

Local testing: point a Resend webhook at a tunnel (e.g. ``ngrok http 8000``)
with the endpoint ``/v1/webhooks/resend``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.services import email_log_service
from app.utils.logger import get_logger

logger = get_logger("email.webhook")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

#: Reject events whose timestamp drifts more than this from our clock —
#: stops replay of captured payloads.
_TOLERANCE_SECONDS = 5 * 60


def verify_svix_signature(
    secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes
) -> bool:
    """Verify a Svix-style webhook signature (the scheme Resend uses)."""
    try:
        if abs(time.time() - int(timestamp)) > _TOLERANCE_SECONDS:
            return False
        key_b64 = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        key = base64.b64decode(key_b64)
        signed = f"{msg_id}.{timestamp}.".encode() + body
        expected = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()
        ).decode()
    except Exception:
        return False
    # Header carries space-separated versioned signatures: "v1,<b64> v1,<b64>".
    for part in signature_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


@router.post("/resend", summary="Resend delivery-status webhook")
async def resend_webhook(request: Request) -> dict[str, bool]:
    secret = get_settings().resend_webhook_secret
    if not secret:
        # Not configured — refuse rather than accept unsigned events.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook not configured",
        )
    body = await request.body()
    ok = verify_svix_signature(
        secret,
        request.headers.get("svix-id", ""),
        request.headers.get("svix-timestamp", ""),
        request.headers.get("svix-signature", ""),
        body,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Bad signature"
        )

    import json

    try:
        event: dict[str, Any] = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed payload") from exc

    event_type = str(event.get("type", ""))
    data = event.get("data") or {}
    provider_id = str(data.get("email_id") or data.get("id") or "")
    try:
        bounced_email = await email_log_service.apply_provider_event(
            event_type, provider_id
        )
        if bounced_email:
            await email_log_service.suppress_announcements(bounced_email)
    except Exception:
        # Ack anyway — retries won't fix a bug, and the error is logged.
        logger.exception("resend webhook handling failed for %s", event_type)
    return {"received": True}


__all__ = ["router", "verify_svix_signature"]
