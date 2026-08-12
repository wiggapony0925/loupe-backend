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
from decimal import Decimal
from typing import Any

import stripe
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User
from app.services import email_service

# How Stripe's subscription statuses map onto our plan state. Stripe's full
# list is: incomplete, incomplete_expired, active, past_due, canceled, unpaid,
# trialing, paused — and it needs three outcomes, not two:
#
#   GRANT (`_ACTIVE_STATUSES`) — the member is entitled to Pro right now.
#     active    paid and current.
#     trialing  inside a trial we granted.
#     past_due  a charge failed but Stripe is still retrying (~2 weeks); pulling
#               access on the first decline punishes a member whose bank will
#               happily re-authorise the very next attempt.
#
#   IGNORE (`_PENDING_STATUSES`) — the subscription has never been paid for, so
#   it says nothing about the member's access: it must neither grant Pro nor
#   take it away. `/me/billing/subscribe` creates subscriptions with
#   `payment_behavior=default_incomplete`, so merely *opening* the in-app
#   payment sheet (switching monthly → yearly, say) emits
#   `customer.subscription.created` with status `incomplete`. Treating that as
#   "not active" downgraded a member who was already paying on another
#   subscription — before they had even entered a card — and repointed
#   `stripe_subscription_id` at the unpaid one, so cancel/reactivate then
#   targeted the wrong subscription.
#     incomplete          initial payment not confirmed yet.
#     incomplete_expired  that first payment was never confirmed and lapsed;
#                         this subscription never granted anything, so there is
#                         nothing for it to revoke.
#
#   REVOKE (everything else) — the subscription is over and no more money is
#   coming: `canceled` (ended by us, the member, or Stripe's final dunning
#   step), `unpaid` (Stripe exhausted its retries), and `paused` (trial ended
#   with no payment method, collection stopped).
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}
_PENDING_STATUSES = {"incomplete", "incomplete_expired"}


# Shown inline by the client whenever Stripe isn't wired up yet. Both purchase
# entry points (hosted checkout and the in-app Payment Element) return it, so
# the same state always has the same copy to render.
_UNAVAILABLE_MESSAGE = (
    "Loupe Pro checkout isn't open yet — you're on the early list "
    "and we'll let you know the moment it goes live."
)


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
        "publishable_key": s.stripe_publishable_key,
        "price_monthly_usd": s.pro_price_monthly_usd,
        "price_yearly_usd": s.pro_price_yearly_usd,
        "trial_days": s.pro_trial_days,
    }


def _price_id(interval: str) -> str:
    s = get_settings()
    price = (
        s.stripe_price_pro_yearly
        if interval == "yearly"
        else s.stripe_price_pro_monthly
    )
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
    customer_params: dict[str, Any] = {
        "email": user.email,
        "metadata": {"user_id": str(user.id)},
    }
    if user.display_name:
        customer_params["name"] = user.display_name
    customer = stripe.Customer.create(**customer_params)
    user.stripe_customer_id = customer.id
    await db.commit()
    await db.refresh(user)
    return customer.id


async def start_checkout(db: AsyncSession, user: User, interval: str) -> dict[str, Any]:
    """Begin a Pro checkout. Returns a checkout URL, or an availability notice
    the client renders inline while Stripe isn't wired yet."""
    if interval not in ("monthly", "yearly"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interval must be 'monthly' or 'yearly'",
        )
    if not billing_configured():
        return {"status": "unavailable", "message": _UNAVAILABLE_MESSAGE}

    _client()
    s = get_settings()
    customer_id = await _ensure_customer(db, user)
    # A new subscriber who never had a trial gets the free trial; returning
    # subscribers don't get a second one.
    sub_data: dict[str, Any] = {"metadata": {"user_id": str(user.id)}}
    if s.pro_trial_days > 0 and user.pro_since is None:
        sub_data["trial_period_days"] = s.pro_trial_days
    trial_note = (
        f"Your first {s.pro_trial_days} days are free — cancel anytime before "
        "then and you won't be charged."
        if sub_data.get("trial_period_days")
        else "Cancel anytime from Settings → Loupe Pro."
    )
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(user.id),
        line_items=[{"price": _price_id(interval), "quantity": 1}],
        allow_promotion_codes=True,
        success_url=s.billing_success_url,
        cancel_url=s.billing_cancel_url,
        subscription_data=sub_data,  # type: ignore[arg-type]
        custom_text={"submit": {"message": trial_note}},
    )
    return {"status": "checkout", "url": session.url}


async def create_subscription(
    db: AsyncSession, user: User, interval: str
) -> dict[str, Any]:
    """Create a subscription up-front (``default_incomplete``) and return the
    client secret the in-app Payment Element confirms — so checkout happens
    inside our own themed UI instead of a redirect.

    With a trial there's no charge now, so Stripe issues a SetupIntent (to vault
    the card for after the trial); otherwise it's a PaymentIntent. Either way the
    ``customer.subscription.*`` webhook is what actually grants the plan.
    """
    if interval not in ("monthly", "yearly"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="interval must be 'monthly' or 'yearly'",
        )
    if not billing_configured():
        # Same shape as start_checkout's: the Payment Element path needs copy to
        # render for this state too, not a bare status the client can't explain.
        return {"status": "unavailable", "message": _UNAVAILABLE_MESSAGE}

    _client()
    s = get_settings()
    customer_id = await _ensure_customer(db, user)
    params: dict[str, Any] = {
        "customer": customer_id,
        "items": [{"price": _price_id(interval)}],
        "payment_behavior": "default_incomplete",
        "payment_settings": {"save_default_payment_method": "on_subscription"},
        "expand": ["pending_setup_intent", "latest_invoice.payment_intent"],
        "metadata": {"user_id": str(user.id)},
    }
    if s.pro_trial_days > 0 and user.pro_since is None:
        params["trial_period_days"] = s.pro_trial_days

    sub = stripe.Subscription.create(**params)
    setup = sub.get("pending_setup_intent")
    if setup:
        return {
            "status": "ready",
            "mode": "setup",
            "client_secret": setup["client_secret"],
            "subscription_id": sub["id"],
        }
    invoice = sub.get("latest_invoice") or {}
    intent = invoice.get("payment_intent") or {}
    return {
        "status": "ready",
        "mode": "payment",
        "client_secret": intent.get("client_secret"),
        "subscription_id": sub["id"],
    }


async def cancel_subscription(
    db: AsyncSession, user: User, *, immediately: bool = False
) -> dict[str, Any]:
    """Admin-side cancel of a user's Stripe subscription.

    Defaults to ``cancel_at_period_end`` (the user keeps Pro until the paid
    period runs out); ``immediately=True`` ends it now. The actual plan
    downgrade is still driven by the ``customer.subscription.*`` webhook — this
    only tells Stripe what to do, keeping Stripe the source of truth.
    """
    if not user.stripe_subscription_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This user has no active Stripe subscription.",
        )
    _client()  # raises 503 when billing isn't configured
    if immediately:
        sub = stripe.Subscription.cancel(user.stripe_subscription_id)
    else:
        sub = stripe.Subscription.modify(
            user.stripe_subscription_id, cancel_at_period_end=True
        )
    period_end = _period_end(sub)
    return {
        "status": sub.get("status"),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "current_period_end": period_end.isoformat() if period_end else None,
    }


async def reactivate_subscription(db: AsyncSession, user: User) -> dict[str, Any]:
    """Undo a scheduled cancellation (clear ``cancel_at_period_end``).

    The self-serve twin of :func:`cancel_subscription` — lets a member who
    changed their mind keep Pro without re-checking out, as long as the paid
    period hasn't lapsed yet. Stripe remains the source of truth.
    """
    if not user.stripe_subscription_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This user has no active Stripe subscription.",
        )
    _client()  # raises 503 when billing isn't configured
    sub = stripe.Subscription.modify(
        user.stripe_subscription_id, cancel_at_period_end=False
    )
    period_end = _period_end(sub)
    return {
        "status": sub.get("status"),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
        "current_period_end": period_end.isoformat() if period_end else None,
    }


async def refund_latest(db: AsyncSession, user: User) -> dict[str, Any]:
    """Refund a user's most recent successful charge (admin support action).

    Issues a full refund of the latest paid, not-yet-refunded charge on the
    user's Stripe customer. Money-out — callers gate this behind super-admin +
    confirmation + audit.
    """
    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This user has no billing account.",
        )
    _client()  # raises 503 when billing isn't configured
    charges = stripe.Charge.list(customer=user.stripe_customer_id, limit=10)
    charge = next(
        (
            c
            for c in charges.get("data", [])
            if c.get("paid")
            and not c.get("refunded")
            and c.get("status") == "succeeded"
            and (c.get("amount", 0) - c.get("amount_refunded", 0)) > 0
        ),
        None,
    )
    if charge is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No refundable charge found for this user.",
        )
    refund = stripe.Refund.create(charge=charge["id"])
    return {
        "refund_id": refund["id"],
        "charge_id": charge["id"],
        "amount_usd": round((refund.get("amount", 0) or 0) / 100, 2),
        "currency": (refund.get("currency") or "usd").upper(),
        "status": refund.get("status") or "pending",
    }


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
        await db.execute(select(User).where(User.stripe_customer_id == customer_id))
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


async def _apply_subscription(
    db: AsyncSession, sub: Any, user: User | None = None
) -> None:
    """Reconcile a user's plan from a Stripe subscription object.

    *user* lets a caller that has already resolved the user skip the lookup by
    customer id. Checkout completion needs that: it may have just assigned
    ``stripe_customer_id`` on a user in this same session, and the sessionmaker
    runs with ``autoflush=False``, so a ``SELECT`` on that column would not see
    the pending assignment and the paid subscription would be dropped.
    """
    if user is None:
        user = await _user_by_customer(db, sub.get("customer"))
    if user is None:
        return
    status_str = sub.get("status")
    if status_str in _PENDING_STATUSES:
        # No-op on plan state (see the status map above): an unpaid subscription
        # neither grants Pro nor revokes it, and must not overwrite
        # `stripe_subscription_id` — that would point cancel/reactivate at a
        # subscription nobody has paid for.
        return
    is_active = status_str in _ACTIVE_STATUSES
    was_pro = user.plan == "pro"
    user.stripe_subscription_id = sub.get("id")
    user.pro_trialing = status_str == "trialing"
    if is_active:
        if user.plan != "pro" or user.pro_since is None:
            user.pro_since = user.pro_since or datetime.now(UTC)
        user.plan = "pro"
        user.pro_expires_at = _period_end(sub)
    else:
        user.plan = "free"
        user.pro_trialing = False
        user.pro_expires_at = _period_end(sub) or datetime.now(UTC)
    await db.commit()
    # Email only on the plan *transition* — subscription.updated fires on
    # every renewal and must stay silent. Best-effort, after commit. The
    # idempotency key makes Stripe's webhook redelivery safe from doubles.
    if is_active and not was_pro:
        await email_service.send_pro_activated(
            user, idempotency_key=f"pro-activated-{sub.get('id')}"
        )
    elif was_pro and not is_active:
        await email_service.send_pro_canceled(
            user, idempotency_key=f"pro-canceled-{sub.get('id')}"
        )


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
        # Persist the link on its own: it is the only key later webhooks have
        # to find this user, so it has to survive even if the reconciliation
        # below turns out to be a no-op (an unpaid subscription, say).
        await db.commit()
    sub_id = session.get("subscription")
    if sub_id:
        sub = stripe.Subscription.retrieve(sub_id)
        # Hand over the user we already resolved rather than making
        # _apply_subscription look them up by customer id again — see its
        # docstring: that lookup cannot see an assignment still pending in this
        # session (autoflush=False), which silently dropped paid checkouts.
        await _apply_subscription(db, sub, user=user)
    else:
        was_pro = user.plan == "pro"
        user.plan = "pro"
        user.pro_since = user.pro_since or datetime.now(UTC)
        await db.commit()
        if not was_pro:
            await email_service.send_pro_activated(
                user, idempotency_key=f"pro-activated-{session.get('id')}"
            )


def _as_uuid(value: str) -> Any:
    import uuid

    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return value


async def _handle_payment_failed(db: AsyncSession, invoice: Any) -> None:
    """Tell the user a charge was declined, before Stripe gives up on it.

    Stripe retries a failed invoice several times over ~2 weeks and only then
    cancels the subscription (which arrives as ``subscription.deleted`` and
    sends the Pro-ended email). Without this notice the whole dunning window
    passes silently and the first thing the user notices is a locked feature.
    """
    user = await _user_by_customer(db, invoice.get("customer"))
    if user is None:
        return
    amount = invoice.get("amount_due")
    next_ts = invoice.get("next_payment_attempt")
    period_end = _as_aware_ts(user.pro_expires_at)
    now = datetime.now(UTC)
    await email_service.send_payment_failed(
        user,
        amount_usd=(Decimal(amount) / 100) if amount else None,
        attempt=int(invoice.get("attempt_count") or 1),
        next_attempt=(datetime.fromtimestamp(next_ts, tz=UTC) if next_ts else None),
        grace_days=(
            max(0, (period_end - now).days) if period_end and period_end > now else None
        ),
        # Per invoice *and* attempt: each retry is a distinct notice, but a
        # redelivered webhook for the same attempt is not.
        idempotency_key=(
            f"payment-failed-{invoice.get('id')}-{invoice.get('attempt_count')}"
        ),
    )


def _as_aware_ts(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on round-trip; treat naive timestamps as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
    elif etype == "invoice.payment_failed":
        await _handle_payment_failed(db, obj)
    # Other events (invoice.paid, etc.) are no-ops: the subscription.* events
    # already carry the period end we need.


__all__ = [
    "billing_configured",
    "cancel_subscription",
    "construct_event",
    "create_portal_session",
    "create_subscription",
    "handle_event",
    "public_config",
    "refund_latest",
    "start_checkout",
]
