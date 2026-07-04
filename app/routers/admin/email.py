"""Admin email surface (`/v1/admin/email`).

The email control room for the dev portal: the template gallery (exact
production renders with sample data), test sends to yourself, the
announcement/support composer, and the delivery log — every send from queue
to delivered/bounced, with the stored render for inspection and one-click
retry of failures.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.config import get_settings
from app.db import get_db
from app.models.user import User
from app.services import audit_service, email_log_service, email_service
from app.services.admin import email_preview_service
from app.services.auth import unsubscribe_service, user_service

router = APIRouter(prefix="/email", tags=["admin-email"])


class EmailTemplateSummary(BaseModel):
    key: str
    label: str
    group: str
    description: str
    subject: str


class EmailStatus(BaseModel):
    enabled: bool
    from_email: str
    reply_to: str
    provider: str = "resend"
    # How many active users currently accept announcement email — what a
    # composed announcement would reach.
    subscribers: int = 0


class EmailTemplatesResponse(BaseModel):
    status: EmailStatus
    templates: list[EmailTemplateSummary]


class EmailTemplateRender(BaseModel):
    key: str
    subject: str
    html: str
    text: str


class TestSendRequest(BaseModel):
    template: str


class TestSendResponse(BaseModel):
    sent: bool
    to: str
    detail: str


class AnnouncementDraft(BaseModel):
    """An admin-composed announcement. Body is plain text — paragraphs split
    on blank lines; no HTML passes through."""

    subject: str = Field(..., min_length=1, max_length=150)
    heading: str = Field(..., min_length=1, max_length=150)
    body: str = Field(..., min_length=1, max_length=5000)
    cta_label: str | None = Field(default=None, max_length=40)
    cta_url: str | None = Field(default=None, max_length=500)

    @field_validator("cta_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("CTA link must be an http(s) URL")
        return v

    def cta(self) -> tuple[str, str] | None:
        if self.cta_label and self.cta_url:
            return (self.cta_label, self.cta_url)
        return None


class AnnouncementSendRequest(AnnouncementDraft):
    # "test" delivers to the acting admin only; "send" queues the real blast.
    mode: str = Field(default="test", pattern="^(test|send)$")


class AnnouncementPreviewRequest(AnnouncementDraft):
    # "announcement" renders with the unsubscribe footer; "support" renders
    # the one-to-one support template (greeting, no footer). Support messages
    # have no heading (the greeting is automatic), so it's optional here.
    kind: str = Field(default="announcement", pattern="^(announcement|support)$")
    heading: str = Field(default="", max_length=150)


class SupportSendRequest(BaseModel):
    """A one-to-one message from support to a specific user."""

    email: EmailStr
    subject: str = Field(..., min_length=1, max_length=150)
    body: str = Field(..., min_length=1, max_length=5000)
    cta_label: str | None = Field(default=None, max_length=40)
    cta_url: str | None = Field(default=None, max_length=500)
    # "test" delivers to the acting admin instead of the user.
    mode: str = Field(default="test", pattern="^(test|send)$")

    @field_validator("cta_url")
    @classmethod
    def _http_only(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("CTA link must be an http(s) URL")
        return v

    def cta(self) -> tuple[str, str] | None:
        if self.cta_label and self.cta_url:
            return (self.cta_label, self.cta_url)
        return None


class AnnouncementSendResponse(BaseModel):
    mode: str
    recipients: int
    detail: str


async def _status(db: AsyncSession) -> EmailStatus:
    s = get_settings()
    return EmailStatus(
        enabled=s.email_enabled,
        from_email=s.notifications_from_email,
        reply_to=s.notifications_reply_to,
        subscribers=len(await user_service.announcement_recipients(db)),
    )


@router.get(
    "/templates",
    response_model=EmailTemplatesResponse,
    summary="Email template gallery + provider status",
)
async def list_templates(db: AsyncSession = Depends(get_db)) -> EmailTemplatesResponse:
    templates = []
    for spec in email_preview_service.TEMPLATES:
        content = spec.render()
        templates.append(
            EmailTemplateSummary(
                key=spec.key,
                label=spec.label,
                group=spec.group,
                description=spec.description,
                subject=content.subject,
            )
        )
    return EmailTemplatesResponse(status=await _status(db), templates=templates)


@router.get(
    "/templates/{key}",
    response_model=EmailTemplateRender,
    summary="Render one template with sample data",
)
async def render_template(key: str) -> EmailTemplateRender:
    spec = email_preview_service.get_template(key)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown template"
        )
    content = spec.render()
    return EmailTemplateRender(
        key=spec.key, subject=content.subject, html=content.html, text=content.text
    )


@router.post(
    "/test",
    response_model=TestSendResponse,
    summary="Send a template to yourself (real delivery)",
)
async def send_test(
    payload: TestSendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> TestSendResponse:
    spec = email_preview_service.get_template(payload.template)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown template"
        )
    if not get_settings().email_enabled:
        return TestSendResponse(
            sent=False,
            to=user.email,
            detail="Email provider not configured — set RESEND_API_KEY and "
            "NOTIFICATIONS_FROM_EMAIL.",
        )
    content = spec.render()
    ok = await email_service.send_email(
        user.email,
        f"[TEST] {content.subject}",
        content.html,
        content.text,
        category="admin-test",
    )
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="email.test_send",
        payload={"template": spec.key, "accepted": ok},
    )
    return TestSendResponse(
        sent=ok,
        to=user.email,
        detail="Accepted by provider."
        if ok
        else "Provider rejected the send — check logs.",
    )


# ── Custom announcements ──────────────────────────────────────────────────


def _sample_unsub_url() -> str:
    return f"{get_settings().api_base_url}/v1/public/unsubscribe?token=preview"


@router.post(
    "/announce/preview",
    response_model=EmailTemplateRender,
    summary="Render a draft announcement or support message (no send)",
)
async def preview_announcement(
    draft: AnnouncementPreviewRequest,
) -> EmailTemplateRender:
    if draft.kind == "support":
        c = email_service.build_support_message(
            recipient_name=None,
            subject=draft.subject,
            body_text=draft.body,
            cta=draft.cta(),
        )
    else:
        c = email_service.build_custom_announcement(
            subject=draft.subject,
            heading=draft.heading,
            body_text=draft.body,
            cta=draft.cta(),
            unsub_url=_sample_unsub_url(),
        )
    return EmailTemplateRender(
        key=draft.kind, subject=c.subject, html=c.html, text=c.text
    )


@router.post(
    "/announce",
    response_model=AnnouncementSendResponse,
    summary="Send a composed announcement — test to yourself, or blast to subscribers",
)
async def send_announcement(
    payload: AnnouncementSendRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> AnnouncementSendResponse:
    if not get_settings().email_enabled:
        return AnnouncementSendResponse(
            mode=payload.mode,
            recipients=0,
            detail="Email provider not configured — set RESEND_API_KEY and "
            "NOTIFICATIONS_FROM_EMAIL.",
        )

    if payload.mode == "test":
        c = email_service.build_custom_announcement(
            subject=payload.subject,
            heading=payload.heading,
            body_text=payload.body,
            cta=payload.cta(),
            unsub_url=_sample_unsub_url(),
        )
        ok = await email_service.send_email(
            user.email, f"[TEST] {c.subject}", c.html, c.text, category="admin-test"
        )
        return AnnouncementSendResponse(
            mode="test",
            recipients=1 if ok else 0,
            detail=f"Test sent to {user.email}."
            if ok
            else "Provider rejected the send.",
        )

    recipients = [
        (email, unsubscribe_service.unsubscribe_url(uid))
        for uid, email in await user_service.announcement_recipients(db)
    ]
    if recipients:
        background.add_task(
            email_service.send_custom_announcement,
            recipients,
            subject=payload.subject,
            heading=payload.heading,
            body_text=payload.body,
            cta=payload.cta(),
        )
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="email.announce",
        payload={"subject": payload.subject, "recipients": len(recipients)},
    )
    return AnnouncementSendResponse(
        mode="send",
        recipients=len(recipients),
        detail=f"Queued for {len(recipients)} subscriber(s).",
    )


# ── Delivery log ──────────────────────────────────────────────────────────


class EmailLogRow(BaseModel):
    id: uuid.UUID
    created_at: datetime
    to_email: str
    user_id: uuid.UUID | None
    category: str | None
    subject: str
    status: str
    error: str | None
    attempts: int
    provider_id: str | None

    model_config = {"from_attributes": True}


class EmailLogDetail(EmailLogRow):
    html: str | None
    text: str | None
    from_email: str | None


class EmailLogPage(BaseModel):
    rows: list[EmailLogRow]
    total: int
    stats: dict[str, int]


@router.get(
    "/log",
    response_model=EmailLogPage,
    summary="Delivery log — every send, queue → delivered/bounced",
)
async def list_log(
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    category: str | None = Query(default=None, max_length=40),
    q: str | None = Query(default=None, max_length=320),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EmailLogPage:
    rows, total = await email_log_service.list_logs(
        db, status=status_filter, category=category, q=q, limit=limit, offset=offset
    )
    return EmailLogPage(
        rows=[EmailLogRow.model_validate(r) for r in rows],
        total=total,
        stats=await email_log_service.stats(db),
    )


@router.get(
    "/log/{log_id}",
    response_model=EmailLogDetail,
    summary="One logged email, including the stored render",
)
async def get_log_entry(
    log_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EmailLogDetail:
    row = await email_log_service.get_log(db, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Log entry not found")
    return EmailLogDetail.model_validate(row)


@router.post(
    "/log/{log_id}/retry",
    response_model=TestSendResponse,
    summary="Re-send a failed (or stuck) email from its stored render",
)
async def retry_log_entry(
    log_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> TestSendResponse:
    row = await email_log_service.get_log(db, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Log entry not found")
    if row.status not in ("failed", "queued"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only failed or stuck sends can be retried (this one is {row.status}).",
        )
    if not row.html:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No stored render to retry.",
        )
    if not get_settings().email_enabled:
        return TestSendResponse(
            sent=False,
            to=row.to_email,
            detail="Email provider not configured — set RESEND_API_KEY and "
            "NOTIFICATIONS_FROM_EMAIL.",
        )
    ok = await email_service.send_email(
        row.to_email,
        row.subject,
        row.html,
        row.text,
        category=row.category,
        headers=row.headers,
        from_email=row.from_email,
        log_id=row.id,
    )
    await audit_service.record(
        db,
        request=request,
        user=user,
        action="email.retry",
        target_table="email_log",
        target_id=row.id,
        payload={"to": row.to_email, "accepted": ok},
    )
    return TestSendResponse(
        sent=ok,
        to=row.to_email,
        detail="Re-sent." if ok else "Provider rejected the send again — check logs.",
    )


# ── Support: message one specific user ────────────────────────────────────


@router.post(
    "/support",
    response_model=TestSendResponse,
    summary="Send a support message to one user (or test it on yourself)",
)
async def send_support(
    payload: SupportSendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> TestSendResponse:
    # Validate the recipient before anything else — "no such account" is the
    # answer the admin needs even when the provider isn't configured yet.
    target = await user_service.get_by_email(db, payload.email)
    if target is None or target.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No account with that email.",
        )
    if not get_settings().email_enabled:
        return TestSendResponse(
            sent=False,
            to=target.email,
            detail="Email provider not configured — set RESEND_API_KEY and "
            "NOTIFICATIONS_FROM_EMAIL.",
        )

    if payload.mode == "test":
        c = email_service.build_support_message(
            recipient_name=target.display_name,
            subject=payload.subject,
            body_text=payload.body,
            cta=payload.cta(),
        )
        ok = await email_service.send_email(
            admin.email,
            f"[TEST] {c.subject}",
            c.html,
            c.text,
            category="admin-test",
            from_email=get_settings().support_sender or None,
        )
        return TestSendResponse(
            sent=ok,
            to=admin.email,
            detail=f"Test sent to {admin.email}."
            if ok
            else "Provider rejected the send.",
        )

    ok = await email_service.send_support_message(
        target, subject=payload.subject, body_text=payload.body, cta=payload.cta()
    )
    await audit_service.record(
        db,
        request=request,
        user=admin,
        action="email.support_send",
        target_table="users",
        target_id=target.id,
        payload={"subject": payload.subject, "accepted": ok},
    )
    return TestSendResponse(
        sent=ok,
        to=target.email,
        detail=f"Sent to {target.email}."
        if ok
        else "Provider rejected the send — check logs.",
    )


__all__ = ["router"]
