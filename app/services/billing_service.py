"""Stripe billing for Loupe Pro — checkout, customer portal, and the webhook
that keeps ``user.plan`` in sync with the subscription.

Designed to be inert until configured: with no ``STRIPE_SECRET_KEY`` every entry
point degrades gracefully (checkout reports ``unavailable``; the webhook 503s)
so the rest of the app — entitlements, the paywall, admin comps — keeps working.
Flip it on by setting the Stripe env vars; nothing else changes.

Lifecycle (the only source of truth for a paid plan is Stripe, relayed here):

    checkout → `checkout.session.completed` links user ⇄ customer/subscription
    billing  → `customer.subscription.created|updated` sets plan + period end
    cancel   → `customer.subscription.deleted` drops the user back to free

Admin comps (``user_admin_service.set_plan``) bypass all of this — they set the
plan directly with no Stripe object, for testers and support.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import stripe
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User

# Subscription statuses that grant Pro. `past_due` stays Pro through Stripe's
# retry window; only a hard cancel/expiry drops access.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def billing_configured() -> bool:
    """True once a Stripe secret key is present (checkout can go live)."""
    return bool(get_settings().stripe_secret_key)


def _client() -> None:
    """Point the Stripe SDK at our key. Raises 503 if billing isn't configured."""
    s = get_settings()
    if not s.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured.",
        )
    stripe.api_key = s.stripe_secret_key


def public_config() -> dict[str, Any]:
    """Pricing + availability the paywall reads to render real numbers."""
    s = get_settings()
    return {
        "checkout_available": billing_configured(),
        "price_monthly_usd": s.pro_price_monthly_usd,
        "price_yearly_usd": s.pro_price_yearly_usd,
    }


def _price_id(interval: str) -> str:
    s = get_settings()
    price = s.stripe_price_pro_yearly if interval == "yearly" else s.stripe_price_pro_monthly
    if not price:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Stripe price configured for the {interval} plan.",
        )
    return price


async def _ensure_customer(db: AsyncSession, user: User) -> str:
    """Return the user's Stripe customer id, creating + persisting it if needed."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        name=user.display_name or None,
        metadata={"user_id": str(user.id)},
    )
    user.stripe_customer_id = customer.id
    await db.commit()
    await db.refresh(user)
    return customer.id


async def start_checkout(
    db: AsyncSession, user: User, interval: str
) -> dict[str, Any]:
    """Begin a Pro checkout. Returns a checkout URL, or an availability notice
    the client renders inline while Stripe isn't wired yet."""
    if interval not in ("monthly", "yearly"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interval must be 'monthly' or 'yearly'",
        )
    if not billing_configured():
        return {
            "status": "unavailable",
            "message": (
                "Loupe Pro checkout isn't open yet — you're on the early list "
                "and we'll let you know the moment it goes live."
            ),
        }

    _client()
    s = get_settings()
    customer_id = await _ensure_customer(db, user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(user.id),
        line_items=[{"price": _price_id(interval), "quantity": 1}],
        allow_promotion_codes=True,
        success_url=s.billing_success_url,
        cancel_url=s.billing_cancel_url,
        subscription_data={"metadata": {"user_id": str(user.id)}},
    )
    return {"status": "checkout", "url": session.url}


async def create_portal_session(db: AsyncSession, user: User) -> dict[str, Any]:
    """A Stripe Customer Portal link so Pro members manage/cancel their plan."""
    if not billing_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured.",
        )
    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No billing account yet — start a subscription first.",
        )
    _client()
    s = get_settings()
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=s.billing_cancel_url,
    )
    return {"url": session.url}


# ── Webhook ──────────────────────────────────────────────────────────────


def construct_event(payload: bytes, signature: str | None) -> stripe.Event:
    """Verify a webhook payload against the signing secret and parse it."""
    s = get_settings()
    if not s.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing webhook is not configured.",
        )
    try:
        return stripe.Webhook.construct_event(
            payload, signature or "", s.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature.",
        ) from exc


async def _user_by_customer(db: AsyncSession, customer_id: str | None) -> User | None:
    if not customer_id:
        return None
    return (
        await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
    ).scalar_one_or_none()


def _period_end(sub: Any) -> datetime | None:
    """Best-effort current-period-end from a subscription object (top-level or
    on the first item, depending on API version)."""
    ts = sub.get("current_period_end")
    if not ts:
        items = (sub.get("items") or {}).get("data") or []
        if items:
            ts = items[0].get("current_period_end")
    return datetime.fromtimestamp(ts, tz=UTC) if ts else None


async def _apply_subscription(db: AsyncSession, sub: Any) -> None:
    """Reconcile a user's plan from a Stripe subscription object."""
    user = await _user_by_customer(db, sub.get("customer"))
    if user is None:
        return
    is_active = sub.get("status") in _ACTIVE_STATUSES
    user.stripe_subscription_id = sub.get("id")
    if is_active:
        if user.plan != "pro" or user.pro_since is None:
            user.pro_since = user.pro_since or datetime.now(UTC)
        user.plan = "pro"
        user.pro_expires_at = _period_end(sub)
    else:
        user.plan = "free"
        user.pro_expires_at = _period_end(sub) or datetime.now(UTC)
    await db.commit()


async def _handle_checkout_completed(db: AsyncSession, session: Any) -> None:
    """Link the user to the customer/subscription created at checkout, then
    pull the subscription to set the initial period end."""
    customer_id = session.get("customer")
    user = await _user_by_customer(db, customer_id)
    if user is None:
        # Fall back to the user id we stamped on the session.
        uid = session.get("client_reference_id")
        if uid:
            user = await db.get(User, _as_uuid(uid))
        if user is None:
            return
        user.stripe_customer_id = customer_id
    sub_id = session.get("subscription")
    if sub_id:
        sub = stripe.Subscription.retrieve(sub_id)
        await _apply_subscription(db, sub)
    else:
        user.plan = "pro"
        user.pro_since = user.pro_since or datetime.now(UTC)
        await db.commit()


def _as_uuid(value: str) -> Any:
    import uuid

    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return value


async def handle_event(db: AsyncSession, event: stripe.Event) -> None:
    """Dispatch a verified Stripe event to the right plan-sync handler."""
    _client()
    etype = event["type"]
    obj = event["data"]["object"]
    if etype == "checkout.session.completed":
        await _handle_checkout_completed(db, obj)
    elif etype in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        await _apply_subscription(db, obj)
    # Other events (invoice.paid, etc.) are no-ops: the subscription.* events
    # already carry the period end we need.


__all__ = [
    "billing_configured",
    "construct_event",
    "create_portal_session",
    "handle_event",
    "public_config",
    "start_checkout",
]
