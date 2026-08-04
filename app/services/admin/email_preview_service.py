"""Admin email-template gallery — render every lifecycle email with sample data.

Powers `/v1/admin/email`: the portal page lists the templates, previews the
exact HTML/text a user would receive (same builders production uses — the
preview can't drift from reality), and can send a real test message to the
acting admin. Sample data is hardcoded, deliberately colorful, and never
touches the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import get_settings
from app.models.career import ApplicationEvent, JobApplication
from app.models.user import User
from app.services import email_service
from app.services.portal import notifications

# Unpersisted stand-in for "a user" in previews — never added to a session.
_SAMPLE_USER = User(email="sam@collector.example", display_name="Sam")

# Fixed "now" for previews so timestamped templates render deterministically.
_SAMPLE_NOW = datetime(2026, 8, 4, 18, 42, tzinfo=UTC)


def _sample_unsub_url() -> str:
    # Deliberately unsigned — preview only. Real sends mint per-recipient
    # tokens in the blog announce path.
    return f"{get_settings().api_base_url}/v1/public/unsubscribe?token=preview"


def _careers_preview() -> email_service.EmailContent:
    application = JobApplication(
        id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
        applicant_name="Sam",
        applicant_email="sam@collector.example",
    )
    event = ApplicationEvent(
        status="interview",
        message="We'd love to talk next week — pick any slot that works.",
    )
    subject, html, text = notifications._build_email(application, event)
    return email_service.EmailContent(subject, html, text)


@dataclass(frozen=True)
class TemplateSpec:
    key: str
    label: str
    group: str
    description: str

    def render(self) -> email_service.EmailContent:
        return _RENDERERS[self.key]()


_RENDERERS = {
    "welcome": lambda: email_service.build_welcome(
        _SAMPLE_USER,
        verify_url=f"{get_settings().api_base_url}/v1/public/verify-email?token=preview",
    ),
    "verify_email": lambda: email_service.build_verify_email(
        _SAMPLE_USER,
        f"{get_settings().api_base_url}/v1/public/verify-email?token=preview",
    ),
    "password_reset": lambda: email_service.build_password_reset(
        _SAMPLE_USER,
        f"{get_settings().app_public_url.rstrip('/')}/reset-password?token=preview",
    ),
    "reset_unavailable": lambda: email_service.build_reset_unavailable(_SAMPLE_USER),
    "password_changed": lambda: email_service.build_password_changed(_SAMPLE_USER),
    "mfa_enabled": lambda: email_service.build_mfa_enabled(_SAMPLE_USER),
    "mfa_disabled": lambda: email_service.build_mfa_disabled(_SAMPLE_USER),
    "new_sign_in": lambda: email_service.build_new_sign_in(
        _SAMPLE_USER,
        device="iPhone 16 Pro · Safari",
        location="Austin, TX, United States",
        ip="203.0.113.42",
        when=_SAMPLE_NOW,
    ),
    "account_locked": lambda: email_service.build_account_locked(
        _SAMPLE_USER, minutes=15, attempts=5, when=_SAMPLE_NOW
    ),
    "pro_activated": lambda: email_service.build_pro_activated(_SAMPLE_USER),
    "pro_canceled": lambda: email_service.build_pro_canceled(_SAMPLE_USER),
    "payment_failed": lambda: email_service.build_payment_failed(
        _SAMPLE_USER,
        amount_usd=Decimal("9.99"),
        attempt=2,
        max_attempts=4,
        next_attempt=_SAMPLE_NOW + timedelta(days=3),
        grace_days=11,
    ),
    "pro_expiring": lambda: email_service.build_pro_expiring(
        _SAMPLE_USER, ends_on=_SAMPLE_NOW + timedelta(days=3), days_left=3
    ),
    "free_limit_reached": lambda: email_service.build_free_limit_reached(
        _SAMPLE_USER, card_count=50, limit=50
    ),
    "portfolio_digest": lambda: email_service.build_portfolio_digest(
        _SAMPLE_USER,
        period_label="This week",
        total_value_usd=Decimal("12480.50"),
        delta_pct=4.2,
        delta_usd=Decimal("503.20"),
        card_count=147,
        unsub_url=_sample_unsub_url(),
        series=[11975, 12040, 11890, 12110, 12260, 12180, 12480.50],
        top_movers=[
            ("Charizard ex #125", 12.4),
            ("Umbreon VMAX #215", 8.1),
            ("Pikachu VMAX #044", -3.6),
        ],
    ),
    "set_completed": lambda: email_service.build_set_completed(
        _SAMPLE_USER,
        set_name="Evolving Skies",
        set_total=237,
        series_name="Sword &amp; Shield",
        set_id="swsh7",
        image_url="https://images.pokemontcg.io/swsh7/logo.png",
        total_value_usd=Decimal("4820.00"),
    ),
    "price_alert": lambda: email_service.build_price_alert(
        card_name="Charizard ex #125",
        set_name="Obsidian Flames",
        condition="above",
        threshold_usd=Decimal("250.00"),
        price_usd=Decimal("262.35"),
        card_id="00000000-0000-0000-0000-000000000000",
        # Real, public catalog art — loads in every mail client.
        image_url="https://images.pokemontcg.io/sv3/125.png",
        history=[
            228.10,
            231.40,
            226.75,
            233.20,
            238.60,
            235.10,
            241.80,
            239.25,
            244.90,
            243.15,
            248.30,
            246.70,
            251.20,
            255.85,
            253.40,
            258.10,
            262.35,
        ],
    ),
    "statement_ready": lambda: email_service.build_statement_ready(
        _SAMPLE_USER,
        title="May 2026 statement",
        total_value_usd=12480.50,
        delta_pct=4.2,
        card_count=147,
        series=[
            11975,
            12040,
            11890,
            12110,
            12260,
            12180,
            12320,
            12290,
            12410,
            12365,
            12440,
            12480.50,
        ],
    ),
    "waitlist_confirmation": lambda: email_service.build_waitlist_confirmation(
        name="Sam", position=42
    ),
    "waitlist_invite": lambda: email_service.build_waitlist_invite(name="Sam"),
    "blog_announcement": lambda: email_service.build_blog_announcement(
        title="Introducing Loupe Grade",
        excerpt=(
            "Pre-screen centering and condition from a photo, and see the "
            "PSA-10 upside before you ship a card off for grading."
        ),
        slug="introducing-loupe-grade",
        unsub_url=_sample_unsub_url(),
        cover_image_url="https://images.pokemontcg.io/sv3/logo.png",
        tag="Product",
        author="The Loupe Team",
        read_minutes=3,
    ),
    "ban_notice": lambda: email_service.build_ban_notice(
        _SAMPLE_USER, "Marketplace spam"
    ),
    "admin_granted": lambda: email_service.build_admin_granted(_SAMPLE_USER),
    "careers_update": _careers_preview,
}

TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        "welcome",
        "Welcome",
        "Lifecycle",
        "Sent on sign-up; password accounts get a confirm-email CTA folded in.",
    ),
    TemplateSpec(
        "verify_email",
        "Confirm your email",
        "Lifecycle",
        "Re-sent on request when an address is still unverified.",
    ),
    TemplateSpec(
        "password_reset",
        "Password reset link",
        "Security",
        "Sent from 'Forgot password?' — single-use link, 30-minute expiry.",
    ),
    TemplateSpec(
        "reset_unavailable",
        "Reset unavailable",
        "Security",
        "Sent when a reset is requested for an Apple/Google-only account.",
    ),
    TemplateSpec(
        "password_changed",
        "Password changed",
        "Security",
        "Sent after a successful password change; other sessions revoked.",
    ),
    TemplateSpec(
        "mfa_enabled",
        "2FA enabled",
        "Security",
        "Sent when two-factor enrollment is confirmed.",
    ),
    TemplateSpec(
        "mfa_disabled",
        "2FA disabled",
        "Security",
        "Sent when two-factor is turned off.",
    ),
    TemplateSpec(
        "new_sign_in",
        "New sign-in",
        "Security",
        "Sent when an account is accessed from an unrecognized device.",
    ),
    TemplateSpec(
        "account_locked",
        "Account locked",
        "Security",
        "Sent when brute-force lockout trips after repeated failed sign-ins.",
    ),
    TemplateSpec(
        "pro_activated",
        "Pro activated",
        "Billing",
        "Sent once when a subscription first grants Pro (renewals are silent).",
    ),
    TemplateSpec(
        "pro_canceled",
        "Pro ended",
        "Billing",
        "Sent once when a subscription lapses back to the free plan.",
    ),
    TemplateSpec(
        "payment_failed",
        "Payment failed",
        "Billing",
        "Dunning notice on a declined charge — Pro is at risk until the card "
        "is updated.",
    ),
    TemplateSpec(
        "pro_expiring",
        "Pro ending soon",
        "Billing",
        "Heads-up before a scheduled cancellation takes effect.",
    ),
    TemplateSpec(
        "free_limit_reached",
        "Vault full",
        "Lifecycle",
        "Sent once when a free-plan vault hits its card ceiling.",
    ),
    TemplateSpec(
        "portfolio_digest",
        "Portfolio digest",
        "Engagement",
        "Recurring collection recap with movers; one-click unsubscribe.",
    ),
    TemplateSpec(
        "set_completed",
        "Set completed",
        "Engagement",
        "Milestone email when every card in a set is owned.",
    ),
    TemplateSpec(
        "price_alert",
        "Price alert fired",
        "Alerts",
        "Sent by the price worker when a user's alert threshold is crossed.",
    ),
    TemplateSpec(
        "statement_ready",
        "Statement ready",
        "Statements",
        "Sent when a monthly/yearly portfolio statement PDF is generated.",
    ),
    TemplateSpec(
        "waitlist_confirmation",
        "Waitlist confirmation",
        "Waitlist",
        "Sent on Scanner waitlist signup with their place in line.",
    ),
    TemplateSpec(
        "waitlist_invite",
        "Waitlist invite",
        "Waitlist",
        "Sent when an admin advances a signup to 'invited'.",
    ),
    TemplateSpec(
        "blog_announcement",
        "Blog announcement",
        "Announcements",
        "Batch-sent to subscribed users on publish; one-click unsubscribe.",
    ),
    TemplateSpec(
        "ban_notice",
        "Account suspended",
        "Account",
        "Sent when an admin bans an account.",
    ),
    TemplateSpec(
        "admin_granted",
        "Admin granted",
        "Account",
        "Sent when a user is given developer-portal access.",
    ),
    TemplateSpec(
        "careers_update",
        "Application update",
        "Careers",
        "Sent when an admin advances a job application (per-stage copy).",
    ),
)

_BY_KEY = {t.key: t for t in TEMPLATES}


def get_template(key: str) -> TemplateSpec | None:
    return _BY_KEY.get(key)


__all__ = ["TEMPLATES", "TemplateSpec", "get_template"]
