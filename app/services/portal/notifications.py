"""Applicant notifications.

When an application's status changes the admin can choose to notify the
applicant. Delivery uses the shared branded template in
:mod:`app.services.email_service` (Resend) when configured via
``RESEND_API_KEY`` + ``NOTIFICATIONS_FROM_EMAIL``; otherwise the intent is
logged and the applicant can still see the update on the public tracking
page. Either way this is best-effort and never raises — a notification
failure must not roll back the status change itself.
"""

from __future__ import annotations

import html
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
) -> tuple[str, str, str]:
    """Return (subject, html, text) for the status-update email."""
    from app.services.email_templates.base import callout, chip, progress_steps

    headline = _STATUS_COPY.get(event.status, "An update on your application")
    status_tone = {
        "interview": "mint",
        "offer": "mint",
        "hired": "mint",
        "rejected": "neutral",
        "withdrawn": "neutral",
    }.get(event.status, "amber")
    # The hiring pipeline as a progress bar; terminal states skip it.
    pipeline_index = {
        "submitted": 0,
        "reviewing": 1,
        "interview": 2,
        "offer": 3,
        "hired": 3,
    }
    pipeline = (
        progress_steps(
            ["Received", "Review", "Interview", "Offer"],
            pipeline_index[event.status],
        )
        if event.status in pipeline_index
        else ""
    )
    note = callout(html.escape(event.message), tone="neutral") if event.message else ""
    body = (
        f"<p>Hi {html.escape(application.applicant_name)} — {headline[0].lower()}"
        f"{headline[1:]}.</p>"
        f"{pipeline}"
        f'<p style="margin:6px 0 0;">{chip(event.status, tone=status_tone)}</p>'
        f"{note}"
    )
    rendered_html, text = email_service.render_email(
        headline + ".",
        body,
        ("View your application", _track_url(application)),
        preheader=headline,
        eyebrow="Your application",
    )
    return f"{headline} — Loupe", rendered_html, text


async def notify_applicant(
    application: JobApplication, event: ApplicationEvent
) -> bool:
    """Email the applicant about a status change. Returns True if accepted by
    the provider, False if it was only logged (no provider / send failed)."""
    subject, rendered_html, text = _build_email(application, event)
    return await email_service.send_email(
        application.applicant_email, subject, rendered_html, text, category="careers"
    )


__all__ = ["notify_applicant"]
