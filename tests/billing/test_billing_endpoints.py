"""HTTP contract for `/v1/me/billing/*` — the money endpoints.

These characterise the rules that protect revenue and access:

* nothing here grants Pro. Checkout and subscribe only *ask* Stripe for a
  payment surface; the plan is written by the webhook alone, so a caller can
  never talk themselves into a paid state.
* every route is caller-scoped — there is no user id in any path, so the worst
  a signed-in user can do is manage their own subscription.
* with Stripe unconfigured (the shipped default) the surface degrades to a
  polite "not open yet" instead of 500s.

Stripe itself is the `fake_stripe` fixture from this package's conftest — no
network, no keys.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import get_settings
from app.models.feature_flag import FeatureFlag
from app.services import entitlement_service
from tests.conftest import assert_envelope_error, assert_envelope_ok

pytestmark = pytest.mark.asyncio


async def _enable_subscriptions(db) -> None:
    """Turn the global gating kill switch ON (off, everyone is Pro)."""
    db.add(
        FeatureFlag(
            key=entitlement_service.SUBSCRIPTIONS_FLAG,
            label="Subscriptions",
            enabled=True,
        )
    )
    await db.commit()


async def _make_subscriber(db, user, *, subscription_id: str = "sub_existing") -> None:
    """A paid member as the webhook would have left them."""
    user.plan = "pro"
    user.pro_since = datetime.now(UTC) - timedelta(days=30)
    user.pro_expires_at = datetime.now(UTC) + timedelta(days=30)
    user.stripe_customer_id = "cus_existing"
    user.stripe_subscription_id = subscription_id
    await db.commit()


# ── GET /v1/me/billing/config ────────────────────────────────────────────


async def test_billing_config_reports_checkout_closed_until_stripe_is_configured(
    client, auth_headers, created_user, billing_off
):
    """The paywall renders real prices even before billing is wired up, so the
    marketing surface never has to hard-code them — it just can't sell yet."""
    data = assert_envelope_ok(
        await client.get("/v1/me/billing/config", headers=auth_headers)
    )
    assert data["checkout_available"] is False
    assert data["price_monthly_usd"] == get_settings().pro_price_monthly_usd
    assert data["price_yearly_usd"] == get_settings().pro_price_yearly_usd


async def test_billing_config_opens_checkout_once_stripe_is_configured(
    client, auth_headers, created_user, fake_stripe
):
    data = assert_envelope_ok(
        await client.get("/v1/me/billing/config", headers=auth_headers)
    )
    assert data["checkout_available"] is True
    assert data["publishable_key"] == "pk_test_fake"


async def test_billing_config_never_leaks_the_secret_key(
    client, auth_headers, created_user, fake_stripe
):
    """Only the *publishable* key may cross the wire. This endpoint is the one
    place both keys are in scope, so it is the one worth pinning."""
    resp = await client.get("/v1/me/billing/config", headers=auth_headers)
    assert_envelope_ok(resp)
    assert "sk_test_fake" not in resp.text


async def test_billing_config_requires_a_session_like_the_rest_of_me(
    client, fake_stripe
):
    """It lives under `/me`, so it answers only a signed-in caller. Today's body
    is public pricing, but the prefix is the access rule clients and reviewers
    read — anything added to it later must be behind auth by default."""
    assert_envelope_error(
        await client.get("/v1/me/billing/config"), expected_status=401
    )


# ── POST /v1/me/billing/checkout ─────────────────────────────────────────


async def test_checkout_rejects_anonymous_callers(client, fake_stripe):
    assert_envelope_error(
        await client.post("/v1/me/billing/checkout", json={"interval": "monthly"}),
        expected_status=401,
    )


async def test_checkout_degrades_gracefully_while_billing_is_off(
    client, auth_headers, created_user, db_session, billing_off
):
    """No Stripe key is a supported state, not an error: the client shows the
    early-list message inline rather than an error toast."""
    data = assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    assert data["status"] == "unavailable"
    assert data["message"]
    # And nothing was written — no half-created billing account.
    await db_session.refresh(created_user)
    assert created_user.stripe_customer_id is None


async def test_checkout_returns_a_hosted_url_and_remembers_the_customer(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """The Stripe customer id is persisted on first checkout — it is the only
    key the webhook has to map an incoming subscription back to a user."""
    data = assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    assert data["status"] == "checkout"
    assert data["url"].startswith("https://checkout.stripe.test/")

    await db_session.refresh(created_user)
    assert created_user.stripe_customer_id == "cus_fake_1"

    created = fake_stripe.last_call("Customer.create")
    assert created["email"] == created_user.email
    assert created["metadata"]["user_id"] == str(created_user.id)

    session = fake_stripe.last_call("checkout.Session.create")
    assert session["line_items"] == [{"price": "price_fake_monthly", "quantity": 1}]
    assert session["client_reference_id"] == str(created_user.id)


async def test_checkout_reuses_an_existing_stripe_customer(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """A second checkout must not mint a second customer — duplicate customers
    split a person's billing history and break the webhook's lookup."""
    created_user.stripe_customer_id = "cus_existing"
    await db_session.commit()

    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    assert "Customer.create" not in fake_stripe.call_names()
    assert (
        fake_stripe.last_call("checkout.Session.create")["customer"] == "cus_existing"
    )


async def test_yearly_checkout_uses_the_yearly_price(
    client, auth_headers, created_user, fake_stripe
):
    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "yearly"},
            headers=auth_headers,
        )
    )
    session = fake_stripe.last_call("checkout.Session.create")
    assert session["line_items"] == [{"price": "price_fake_yearly", "quantity": 1}]


async def test_checkout_rejects_an_unknown_interval(
    client, auth_headers, created_user, fake_stripe
):
    """Guarded at the schema, before any Stripe call — an unrecognised interval
    would otherwise fall through to the monthly price and mis-bill."""
    assert_envelope_error(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "weekly"},
            headers=auth_headers,
        ),
        expected_status=422,
    )
    assert fake_stripe.calls == []


async def test_checkout_is_unavailable_when_the_price_is_not_configured(
    client, auth_headers, created_user, fake_stripe, monkeypatch
):
    """Key set but prices missing is a half-configured deploy: 503 beats
    selling at whatever price Stripe defaults to."""
    monkeypatch.setattr(get_settings(), "stripe_price_pro_yearly", "")
    assert_envelope_error(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "yearly"},
            headers=auth_headers,
        ),
        expected_status=503,
    )


async def test_first_subscriber_gets_a_trial_and_a_returning_one_does_not(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """`pro_since` is the "has ever been Pro" marker. Without this check a
    member could cancel and re-subscribe for an unlimited run of free trials."""
    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    first = fake_stripe.last_call("checkout.Session.create")
    assert first["subscription_data"]["trial_period_days"] == (
        get_settings().pro_trial_days
    )

    created_user.pro_since = datetime.now(UTC) - timedelta(days=365)
    await db_session.commit()

    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    second = fake_stripe.last_call("checkout.Session.create")
    assert "trial_period_days" not in second["subscription_data"]


async def test_starting_checkout_does_not_make_you_a_subscriber(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """The whole security model of billing: only the signed webhook grants Pro.
    Reaching checkout is not paying for it."""
    await _enable_subscriptions(db_session)
    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/checkout",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    await db_session.refresh(created_user)
    assert created_user.plan == "free"

    ent = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert ent["is_pro"] is False


# ── POST /v1/me/billing/subscribe (in-app Payment Element) ───────────────


async def test_subscribe_rejects_anonymous_callers(client, fake_stripe):
    assert_envelope_error(
        await client.post("/v1/me/billing/subscribe", json={"interval": "monthly"}),
        expected_status=401,
    )


async def test_subscribe_reports_unavailable_while_billing_is_off(
    client, auth_headers, created_user, billing_off
):
    data = assert_envelope_ok(
        await client.post(
            "/v1/me/billing/subscribe",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    assert data["status"] == "unavailable"


async def test_subscribe_returns_a_setup_secret_when_a_trial_defers_the_charge(
    client, auth_headers, created_user, fake_stripe
):
    """Nothing is owed during a trial, so Stripe vaults the card via a
    SetupIntent. The client has to know which intent it is confirming."""
    data = assert_envelope_ok(
        await client.post(
            "/v1/me/billing/subscribe",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    assert data["status"] == "ready"
    assert data["mode"] == "setup"
    assert data["client_secret"] == "seti_fake_secret"
    assert data["subscription_id"] == "sub_fake_1"

    params = fake_stripe.last_call("Subscription.create")
    assert params["payment_behavior"] == "default_incomplete"
    assert params["items"] == [{"price": "price_fake_monthly"}]


async def test_subscribe_returns_a_payment_secret_when_the_charge_is_immediate(
    client, auth_headers, created_user, fake_stripe, monkeypatch
):
    monkeypatch.setattr(get_settings(), "pro_trial_days", 0)
    data = assert_envelope_ok(
        await client.post(
            "/v1/me/billing/subscribe",
            json={"interval": "yearly"},
            headers=auth_headers,
        )
    )
    assert data["mode"] == "payment"
    assert data["client_secret"] == "pi_fake_secret"
    assert "trial_period_days" not in fake_stripe.last_call("Subscription.create")


async def test_subscribe_rejects_an_unknown_interval(
    client, auth_headers, created_user, fake_stripe
):
    assert_envelope_error(
        await client.post(
            "/v1/me/billing/subscribe",
            json={"interval": "lifetime"},
            headers=auth_headers,
        ),
        expected_status=422,
    )
    assert fake_stripe.calls == []


async def test_creating_a_subscription_does_not_make_you_a_subscriber(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """`default_incomplete` means Stripe has not been paid yet — the plan waits
    for `customer.subscription.*`, no matter what this endpoint returned."""
    await _enable_subscriptions(db_session)
    assert_envelope_ok(
        await client.post(
            "/v1/me/billing/subscribe",
            json={"interval": "monthly"},
            headers=auth_headers,
        )
    )
    await db_session.refresh(created_user)
    assert created_user.plan == "free"
    assert created_user.stripe_subscription_id is None


# ── POST /v1/me/billing/portal ───────────────────────────────────────────


async def test_portal_rejects_anonymous_callers(client, fake_stripe):
    assert_envelope_error(
        await client.post("/v1/me/billing/portal"), expected_status=401
    )


async def test_portal_is_unavailable_while_billing_is_off(
    client, auth_headers, created_user, billing_off
):
    """Unlike checkout, the portal has no graceful fallback to show — there is
    nothing to manage without Stripe, so it is an honest 503."""
    assert_envelope_error(
        await client.post("/v1/me/billing/portal", headers=auth_headers),
        expected_status=503,
    )


async def test_portal_refuses_a_user_with_no_billing_account(
    client, auth_headers, created_user, fake_stripe
):
    assert_envelope_error(
        await client.post("/v1/me/billing/portal", headers=auth_headers),
        expected_status=400,
    )
    assert "billing_portal.Session.create" not in fake_stripe.call_names()


async def test_portal_opens_only_the_callers_own_billing_account(
    client, auth_headers, created_user, second_user, db_session, fake_stripe
):
    """The portal exposes invoices and cards, so the customer id must come from
    the token's user and nothing else — there is no id in the request at all."""
    created_user.stripe_customer_id = "cus_mine"
    second_user.stripe_customer_id = "cus_theirs"
    await db_session.commit()

    data = assert_envelope_ok(
        await client.post("/v1/me/billing/portal", headers=auth_headers)
    )
    assert data["url"].startswith("https://portal.stripe.test/")
    assert fake_stripe.last_call("billing_portal.Session.create")["customer"] == (
        "cus_mine"
    )


# ── POST /v1/me/billing/cancel ───────────────────────────────────────────


async def test_cancel_rejects_anonymous_callers(client, fake_stripe):
    assert_envelope_error(
        await client.post("/v1/me/billing/cancel"), expected_status=401
    )


async def test_cancel_refuses_a_user_who_has_no_subscription(
    client, auth_headers, created_user, fake_stripe
):
    """A free account has nothing to cancel; answering 400 (rather than a
    cheerful 200) keeps the client from showing a bogus "cancelled" state."""
    assert_envelope_error(
        await client.post("/v1/me/billing/cancel", headers=auth_headers),
        expected_status=400,
    )
    assert fake_stripe.calls == []


async def test_cancel_needs_stripe_even_when_the_user_has_a_subscription(
    client, auth_headers, created_user, db_session, billing_off
):
    """Never pretend a cancellation happened: with billing unconfigured we
    cannot tell Stripe anything, so the request fails loudly."""
    await _make_subscriber(db_session, created_user)
    assert_envelope_error(
        await client.post("/v1/me/billing/cancel", headers=auth_headers),
        expected_status=503,
    )


async def test_cancelling_a_subscription_keeps_access_until_period_end(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """Members paid through the end of the period, so cancel schedules
    `cancel_at_period_end` instead of ending access on the spot — and the plan
    itself is left alone until Stripe confirms via the webhook."""
    await _enable_subscriptions(db_session)
    await _make_subscriber(db_session, created_user)

    data = assert_envelope_ok(
        await client.post("/v1/me/billing/cancel", headers=auth_headers)
    )
    assert data["cancel_at_period_end"] is True
    assert data["status"] == "active"
    assert data["current_period_end"] is not None
    assert fake_stripe.last_call("Subscription.modify") == {
        "id": "sub_existing",
        "cancel_at_period_end": True,
    }
    assert "Subscription.cancel" not in fake_stripe.call_names()

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    ent = assert_envelope_ok(
        await client.get("/v1/me/entitlements", headers=auth_headers)
    )
    assert ent["is_pro"] is True


async def test_cancel_is_idempotent(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """Double-taps and retries are normal on mobile. A second cancel must be a
    no-op that reports the same state, never an error or a double action."""
    await _make_subscriber(db_session, created_user)

    first = assert_envelope_ok(
        await client.post("/v1/me/billing/cancel", headers=auth_headers)
    )
    second = assert_envelope_ok(
        await client.post("/v1/me/billing/cancel", headers=auth_headers)
    )
    assert first == second

    await db_session.refresh(created_user)
    assert created_user.plan == "pro"


async def test_cancel_only_ever_touches_the_callers_own_subscription(
    client, second_user_headers, created_user, second_user, db_session, fake_stripe
):
    """Cross-tenant guard: the subscription id is read off the token's user, so
    a bystander cannot cancel someone else's Pro."""
    await _make_subscriber(db_session, created_user)

    assert_envelope_error(
        await client.post("/v1/me/billing/cancel", headers=second_user_headers),
        expected_status=400,
    )
    assert fake_stripe.calls == []
    await db_session.refresh(created_user)
    assert created_user.plan == "pro"
    assert created_user.stripe_subscription_id == "sub_existing"


# ── POST /v1/me/billing/reactivate ───────────────────────────────────────


async def test_reactivate_rejects_anonymous_callers(client, fake_stripe):
    assert_envelope_error(
        await client.post("/v1/me/billing/reactivate"), expected_status=401
    )


async def test_reactivate_refuses_a_user_who_never_subscribed(
    client, auth_headers, created_user, fake_stripe
):
    """Reactivate resumes an existing subscription; it is not a back door into
    Pro for someone who never paid."""
    assert_envelope_error(
        await client.post("/v1/me/billing/reactivate", headers=auth_headers),
        expected_status=400,
    )
    assert fake_stripe.calls == []


async def test_reactivate_clears_a_scheduled_cancellation(
    client, auth_headers, created_user, db_session, fake_stripe
):
    """Changing your mind before the period ends keeps the same subscription —
    no second checkout, and no gap in access."""
    await _make_subscriber(db_session, created_user)

    cancelled = assert_envelope_ok(
        await client.post("/v1/me/billing/cancel", headers=auth_headers)
    )
    assert cancelled["cancel_at_period_end"] is True

    resumed = assert_envelope_ok(
        await client.post("/v1/me/billing/reactivate", headers=auth_headers)
    )
    assert resumed["cancel_at_period_end"] is False
    assert fake_stripe.last_call("Subscription.modify") == {
        "id": "sub_existing",
        "cancel_at_period_end": False,
    }


async def test_reactivate_needs_stripe_to_be_configured(
    client, auth_headers, created_user, db_session, billing_off
):
    await _make_subscriber(db_session, created_user)
    assert_envelope_error(
        await client.post("/v1/me/billing/reactivate", headers=auth_headers),
        expected_status=503,
    )
