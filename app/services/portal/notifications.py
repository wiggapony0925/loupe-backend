"""Applicant notifications.

When an application's status changes the admin can choose to notify the
applicant. Delivery uses Resend (a cheap, single-POST transactional-email
API) when configured via ``RESEND_API_KEY`` + ``NOTIFICATIONS_FROM_EMAIL``;
otherwise the intent is logged and the applicant can still see the update on
the public tracking page. Either way this is best-effort and never raises —
a notification failure must not roll back the status change itself.
"""

from __future__ import annotations

from urllib.parse import quote

from app.config import get_settings
from app.models.career import ApplicationEvent, JobApplication
from app.services import email_service
from app.utils.logger import get_logger

logger = get_logger("portal.notifications")

# Friendly, applicant-facing copy per pipeline stage.
_STATUS_COPY: dict[str, str] = {
    "submitted": "We've received your application",
    "reviewing": "Your application is under review",
    "interview": "We'd like to interview you",
    "offer": "We're making you an offer",
    "hired": "Welcome to the team!",
    "rejected": "An update on your application",
    "withdrawn": "Your application was withdrawn",
}


def _track_url(application: JobApplication) -> str:
    base = get_settings().app_public_url.rstrip("/")
    return f"{base}/careers/track?id={application.id}&email={quote(application.applicant_email)}"


def _build_email(
    application: JobApplication, event: ApplicationEvent
) -> tuple[str, str]:
    """Return (subject, html) for the status-update email."""
    headline = _STATUS_COPY.get(event.status, "An update on your application")
    subject = f"{headline} — Loupe"
    track = _track_url(application)
    note = (
        f'<p style="margin:16px 0;padding:12px 16px;background:#f5f5f7;'
        f'border-radius:10px;color:#1c1c1e;">{event.message}</p>'
        if event.message
        else ""
    )
    html = (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        f'max-width:520px;margin:0 auto;color:#1c1c1e;">'
        f'<h2 style="color:#0b0b0d;">Hi {application.applicant_name},</h2>'
        f"<p>{headline}.</p>"
        f"{note}"
        f'<p style="margin:24px 0;">'
        f'<a href="{track}" style="background:#00a86e;color:#fff;text-decoration:none;'
        f'padding:12px 20px;border-radius:10px;font-weight:600;">View your application</a></p>'
        f'<p style="color:#6e6e73;font-size:13px;">— The Loupe team</p>'
        f"</div>"
    )
    return subject, html


async def notify_applicant(
    application: JobApplication, event: ApplicationEvent
) -> bool:
    """Email the applicant about a status change. Returns True if accepted by
    the provider, False if it was only logged (no provider / send failed)."""
    subject, html = _build_email(application, event)
    return await email_service.send_email(application.applicant_email, subject, html)


__all__ = ["notify_applicant"]
