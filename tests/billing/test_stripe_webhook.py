"""HTTP contract for `POST /v1/billing/webhook` — the only route that grants Pro.

Stripe calls this server-to-server, so there is no session and no bearer token:
the *signature* is the credential. Everything below is therefore about one
question — can an unsigned or unverifiable request move a user's plan? — plus
the reconciliation the verified events are supposed to perform.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.services import billing_service
from tests.billing.conftest import PERIOD_END_TS
from tests.conftest import assert_envelope_error, assert_envelope_ok

pytestmark = pytest.mark.asyncio


def _subscription_event(
    etype: str, *, customer: str, status: str, sub_id: str = "sub_hook_1"
) -> dict:
    return {
        "id": "evt_1",
        "type": etype,
        "data": {
            "object": {
                "id": sub_id,
                "customer": customer,
                "status": status,
                "current_period_end": PERIOD_END_TS,
            }
        },
    }


async def _post_hook(client, *, signed: bool = True):
    headers = {"stripe-signature": "t=1,v1=deadbeef"} if signed else {}
    return await client.post(
        "/v1/billing/webhook", content=b'{"id":"evt_1"}', headers=headers
    )


# ── The signature is the only credential ─────────────────────────────────


async def test_webhook_rejects_a_payload_with_no_signature(
    client, created_user, db_session, fake_stripe
):
    """No header, no trust. An attacker who knows the URL must not be able to
    hand themselves a subscription by POSTing a plausible event body."""
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_hook", status="active"
    )

    assert_envelope_error(await _post_hook(client, signed=False), expected_status=400)

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


async def test_webhook_rejects_a_forged_signature(
    client, created_user, db_session, fake_stripe
):
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.valid_signature = False
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_hook", status="active"
    )

    assert_envelope_error(await _post_hook(client), expected_status=400)

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


async def test_webhook_is_unavailable_until_the_signing_secret_is_set(
    client, billing_off
):
    """With no secret there is no way to verify anything, so the endpoint
    refuses rather than trusting the payload."""
    assert_envelope_error(await _post_hook(client), expected_status=503)


async def test_webhook_verifies_against_the_configured_secret(client, fake_stripe):
    """Pins *which* secret is used — verifying against anything other than the
    webhook signing secret would accept forged events."""
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_nobody", status="active"
    )
    assert_envelope_ok(await _post_hook(client))
    assert fake_stripe.last_call("Webhook.construct_event")["secret"] == "whsec_fake"


# ── Plan reconciliation ──────────────────────────────────────────────────


async def test_a_verified_subscription_event_grants_pro(
    client, created_user, db_session, fake_stripe
):
    """The happy path of the entire billing system: Stripe says the customer is
    active, so the matching user becomes Pro with the period end recorded."""
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created", customer="cus_hook", status="active"
    )

    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_hook_1"
    assert created_user.pro_since is not None
    assert created_user.pro_expires_at is not None


async def test_a_deleted_subscription_drops_the_user_back_to_free(
    client, created_user, db_session, fake_stripe
):
    created_user.stripe_customer_id = "cus_hook"
    created_user.plan = "pro"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.deleted", customer="cus_hook", status="canceled"
    )

    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "free"
    assert created_user.pro_trialing is False


async def test_a_past_due_subscription_keeps_pro_through_the_retry_window(
    client, created_user, db_session, fake_stripe
):
    """A failed charge is not a cancellation — Stripe retries for ~2 weeks, and
    yanking access on the first decline would punish a member for an expired
    card the bank will happily re-authorise."""
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_hook", status="past_due"
    )

    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"


async def test_an_event_for_an_unknown_customer_is_acknowledged_and_ignored(
    client, created_user, db_session, fake_stripe
):
    """Events for customers we've never seen (another environment sharing the
    account, a deleted user) must not 500 — and must not touch anyone else."""
    created_user.stripe_customer_id = "cus_mine"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created", customer="cus_someone_else", status="active"
    )

    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


def _checkout_completed_event(user_id, *, customer: str, subscription: str | None):
    obj = {
        "id": "cs_1",
        "customer": customer,
        "client_reference_id": str(user_id),
    }
    if subscription:
        obj["subscription"] = subscription
    return {
        "id": "evt_2",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }


async def test_checkout_completion_activates_pro_for_a_linked_customer(
    client, created_user, db_session, fake_stripe
):
    """The normal path: `start_checkout` stored the customer id up front, so
    the completion event resolves the user and pulls the subscription for its
    period end."""
    created_user.stripe_customer_id = "cus_new"
    await db_session.commit()
    fake_stripe.retrieved_subscription = {
        "id": "sub_hook_2",
        "customer": "cus_new",
        "status": "active",
        "current_period_end": PERIOD_END_TS,
    }
    fake_stripe.next_event = _checkout_completed_event(
        created_user.id, customer="cus_new", subscription="sub_hook_2"
    )

    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_hook_2"
    assert created_user.pro_expires_at is not None


async def test_checkout_completion_links_and_activates_an_unlinked_user(
    client, created_user, db_session, fake_stripe
):
    """A paid checkout must land even when we have never stored the customer id.

    The handler falls back to the session's `client_reference_id`, links the
    customer to that user, and reconciles the subscription against *that* user —
    it does not re-look-them-up by a customer id that is still only pending in
    this session (the sessionmaker runs with `autoflush=False`, so such a lookup
    finds nobody and the purchase would be dropped with a cheerful 200).
    """
    fake_stripe.retrieved_subscription = {
        "id": "sub_hook_2",
        "customer": "cus_new",
        "status": "active",
        "current_period_end": PERIOD_END_TS,
    }
    fake_stripe.next_event = _checkout_completed_event(
        created_user.id, customer="cus_new", subscription="sub_hook_2"
    )

    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}

    await db_session.refresh(created_user)
    assert created_user.stripe_customer_id == "cus_new"
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_hook_2"
    assert created_user.pro_since is not None
    assert created_user.pro_expires_at is not None


async def test_checkout_completion_links_the_customer_even_if_the_sub_is_unpaid(
    client, created_user, db_session, fake_stripe
):
    """The customer link is the only key later webhooks have to find this user,
    so it is persisted on its own — a subscription that is still `incomplete`
    (delayed payment method) is a no-op on the plan but must not cost us the
    link, or the eventual payment has no user to credit."""
    fake_stripe.retrieved_subscription = {
        "id": "sub_hook_3",
        "customer": "cus_new",
        "status": "incomplete",
        "current_period_end": PERIOD_END_TS,
    }
    fake_stripe.next_event = _checkout_completed_event(
        created_user.id, customer="cus_new", subscription="sub_hook_3"
    )

    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.stripe_customer_id == "cus_new"
    assert created_user.plan == "free"
    assert created_user.stripe_subscription_id is None


async def test_checkout_completion_without_a_subscription_links_and_grants(
    client, created_user, db_session, fake_stripe
):
    """The same `client_reference_id` fallback *does* work on the branch that
    commits directly — which is what isolates the failure above to the
    lookup-by-customer round trip rather than the fallback itself."""
    fake_stripe.next_event = _checkout_completed_event(
        created_user.id, customer="cus_new", subscription=None
    )

    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.stripe_customer_id == "cus_new"
    assert created_user.plan == "pro"
    assert created_user.pro_since is not None


async def test_an_unrelated_event_type_is_a_no_op(
    client, created_user, db_session, fake_stripe
):
    """Stripe fans out far more events than we subscribe to; the unhandled ones
    must be cheap acknowledgements, not errors."""
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.next_event = {
        "id": "evt_3",
        "type": "invoice.paid",
        "data": {"object": {"customer": "cus_hook"}},
    }

    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


async def test_an_incomplete_subscription_leaves_a_paying_member_alone(
    client, created_user, db_session, fake_stripe
):
    """`incomplete` means "payment not confirmed yet", which is neither a grant
    nor a revocation — it is a no-op on plan state.

    `/me/billing/subscribe` creates subscriptions with `default_incomplete`, so
    a member who merely opens the in-app payment sheet again (switching monthly
    → yearly, say) triggers `customer.subscription.created` with that status.
    Treating it as "not active" would strip the Pro they are already paying for
    before they had even entered a card, and repoint `stripe_subscription_id`
    at the unpaid subscription so cancel/reactivate hit the wrong one.
    """
    created_user.stripe_customer_id = "cus_hook"
    created_user.plan = "pro"
    created_user.stripe_subscription_id = "sub_paid"
    await db_session.commit()

    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created",
        customer="cus_hook",
        status="incomplete",
        sub_id="sub_unpaid",
    )
    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_paid"


async def test_an_incomplete_subscription_does_not_grant_pro_either(
    client, created_user, db_session, fake_stripe
):
    """The other half of the no-op rule: an unpaid subscription must not hand a
    free account Pro while Stripe waits for the first payment to confirm."""
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()

    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created",
        customer="cus_hook",
        status="incomplete",
        sub_id="sub_unpaid",
    )
    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "free"
    assert created_user.stripe_subscription_id is None


async def test_an_expired_incomplete_subscription_does_not_revoke_pro(
    client, created_user, db_session, fake_stripe
):
    """`incomplete_expired` is where an abandoned payment sheet ends up. That
    subscription never granted anything, so it has nothing to take away — the
    member's paid subscription is untouched."""
    created_user.stripe_customer_id = "cus_hook"
    created_user.plan = "pro"
    created_user.stripe_subscription_id = "sub_paid"
    await db_session.commit()

    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated",
        customer="cus_hook",
        status="incomplete_expired",
        sub_id="sub_unpaid",
    )
    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_paid"


async def test_an_unpaid_subscription_revokes_access(
    client, created_user, db_session, fake_stripe
):
    """`unpaid` is Stripe giving up after the whole dunning window — unlike
    `past_due` (still retrying) it means no more money is coming, so access
    ends."""
    created_user.stripe_customer_id = "cus_hook"
    created_user.plan = "pro"
    await db_session.commit()

    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_hook", status="unpaid"
    )
    assert_envelope_ok(await _post_hook(client))

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


# ── Failure handling: a 200 is a promise we processed the event ──────────


async def test_a_handler_crash_answers_5xx_so_stripe_retries(
    client, created_user, db_session, fake_stripe, monkeypatch
):
    """200 tells Stripe never to redeliver, so it may only be sent for an event
    we actually handled. An unexpected failure (a DB blip, a lock timeout) is
    transient until proven otherwise: answering 5xx puts the event back on
    Stripe's retry schedule instead of losing the subscription for good."""
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created", customer="cus_hook", status="active"
    )
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()

    async def _boom(db, event):
        raise RuntimeError("transient failure")

    monkeypatch.setattr(billing_service, "handle_event", _boom)

    assert_envelope_error(await _post_hook(client), expected_status=500)

    await db_session.refresh(created_user)
    assert created_user.plan == "free"  # nothing half-applied; Stripe retries


async def test_a_half_configured_deploy_asks_stripe_to_retry(
    client, created_user, db_session, fake_stripe, monkeypatch
):
    """Signing secret set but no API key: `handle_event` 503s on its first line.
    That is the most likely way to lose real payments, so the status is passed
    through to Stripe — the event waits in the retry queue until the deploy is
    fixed, instead of being acknowledged and dropped."""
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "")
    created_user.stripe_customer_id = "cus_hook"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.created", customer="cus_hook", status="active"
    )

    assert_envelope_error(await _post_hook(client), expected_status=503)

    await db_session.refresh(created_user)
    assert created_user.plan == "free"


async def test_a_genuine_no_op_is_still_acknowledged_with_200(
    client, created_user, db_session, fake_stripe
):
    """The flip side: events we deliberately do nothing with — an unknown
    customer, an event type we don't subscribe to — are fully handled, so they
    get their 200 and Stripe stops. Only failures earn a retry."""
    created_user.stripe_customer_id = "cus_mine"
    await db_session.commit()
    fake_stripe.next_event = _subscription_event(
        "customer.subscription.updated", customer="cus_someone_else", status="active"
    )
    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}

    fake_stripe.next_event = {
        "id": "evt_9",
        "type": "customer.updated",
        "data": {"object": {"customer": "cus_mine"}},
    }
    assert assert_envelope_ok(await _post_hook(client)) == {"received": True}
