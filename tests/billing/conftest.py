"""Billing-area fixtures: a fake Stripe SDK and the two config states.

Money is the one place a test must never be at the mercy of the environment.
``billing_service`` imports the ``stripe`` SDK at module scope and reads its
keys from the settings singleton, so those two attributes are the whole seam:
swap the module for a recorder and pin the keys, and every checkout, portal,
cancel and webhook runs offline and deterministically — no network, no keys,
and no dependence on whether the developer's ``.env`` happens to carry a live
``STRIPE_SECRET_KEY`` today.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import get_settings
from app.services import billing_service

#: A fixed "30 days from now" so period-end assertions never race the clock.
PERIOD_END_TS = 2_000_000_000  # 2033-05-18T03:33:20Z


class FakeSignatureVerificationError(Exception):
    """Stands in for ``stripe.SignatureVerificationError``."""


class FakeStripe:
    """A recording stand-in for the ``stripe`` module.

    Only the surface ``billing_service`` actually touches is implemented, and
    every call lands in :attr:`calls` so a test can assert on *what we asked
    Stripe to do* — which, for cancel/reactivate, is the entire behaviour: the
    plan change itself only ever arrives back through the webhook.
    """

    def __init__(self) -> None:
        self.api_key: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.SignatureVerificationError = FakeSignatureVerificationError
        #: What ``Webhook.construct_event`` should return once verified.
        self.next_event: dict[str, Any] | None = None
        #: Flip to False to simulate a forged/misconfigured signature.
        self.valid_signature = True
        #: What ``Subscription.retrieve`` hands back to the checkout handler.
        self.retrieved_subscription: dict[str, Any] = {}
        self.cancel_at_period_end = False

        self.Customer = SimpleNamespace(create=self._customer_create)
        self.checkout = SimpleNamespace(
            Session=SimpleNamespace(create=self._checkout_session_create)
        )
        self.billing_portal = SimpleNamespace(
            Session=SimpleNamespace(create=self._portal_session_create)
        )
        self.Subscription = SimpleNamespace(
            create=self._subscription_create,
            modify=self._subscription_modify,
            cancel=self._subscription_cancel,
            retrieve=self._subscription_retrieve,
        )
        self.Webhook = SimpleNamespace(construct_event=self._construct_event)

    # ── recording ────────────────────────────────────────────────────────

    def _record(self, call: str, kwargs: dict[str, Any]) -> None:
        # The call name is passed positionally: Stripe params include ``name``
        # and ``id``, which would collide with any **kwargs signature here.
        self.calls.append((call, kwargs))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def last_call(self, name: str) -> dict[str, Any]:
        """Kwargs of the most recent *name* call (assert we asked for the right
        thing — the price id, the trial, the cancel flag)."""
        for call_name, kwargs in reversed(self.calls):
            if call_name == name:
                return kwargs
        raise AssertionError(f"{name} was never called; saw {self.call_names()}")

    # ── the SDK surface billing_service uses ─────────────────────────────

    def _customer_create(self, **kwargs: Any) -> SimpleNamespace:
        self._record("Customer.create", kwargs)
        return SimpleNamespace(id="cus_fake_1")

    def _checkout_session_create(self, **kwargs: Any) -> SimpleNamespace:
        self._record("checkout.Session.create", kwargs)
        return SimpleNamespace(
            id="cs_fake_1", url="https://checkout.stripe.test/c/cs_fake_1"
        )

    def _portal_session_create(self, **kwargs: Any) -> SimpleNamespace:
        self._record("billing_portal.Session.create", kwargs)
        return SimpleNamespace(url="https://portal.stripe.test/p/bps_fake_1")

    def _subscription_create(self, **kwargs: Any) -> dict[str, Any]:
        self._record("Subscription.create", kwargs)
        # Mirrors Stripe: a trialing subscription owes nothing now, so it comes
        # back with a SetupIntent to vault the card; a billed one has a
        # PaymentIntent on the first invoice.
        if kwargs.get("trial_period_days"):
            return {
                "id": "sub_fake_1",
                "pending_setup_intent": {"client_secret": "seti_fake_secret"},
            }
        return {
            "id": "sub_fake_1",
            "pending_setup_intent": None,
            "latest_invoice": {"payment_intent": {"client_secret": "pi_fake_secret"}},
        }

    def _subscription_modify(self, subscription_id: str, **kwargs: Any) -> dict:
        self._record("Subscription.modify", {"id": subscription_id, **kwargs})
        if "cancel_at_period_end" in kwargs:
            self.cancel_at_period_end = bool(kwargs["cancel_at_period_end"])
        return {
            "id": subscription_id,
            "status": "active",
            "cancel_at_period_end": self.cancel_at_period_end,
            "current_period_end": PERIOD_END_TS,
        }

    def _subscription_cancel(self, subscription_id: str, **kwargs: Any) -> dict:
        self._record("Subscription.cancel", {"id": subscription_id, **kwargs})
        return {
            "id": subscription_id,
            "status": "canceled",
            "cancel_at_period_end": False,
            "current_period_end": PERIOD_END_TS,
        }

    def _subscription_retrieve(self, subscription_id: str, **kwargs: Any) -> dict:
        self._record("Subscription.retrieve", {"id": subscription_id, **kwargs})
        return self.retrieved_subscription

    def _construct_event(
        self, payload: bytes, signature: str, secret: str
    ) -> dict[str, Any]:
        self._record(
            "Webhook.construct_event",
            {"payload": payload, "signature": signature, "secret": secret},
        )
        if not self.valid_signature or not signature:
            raise FakeSignatureVerificationError("bad signature")
        assert self.next_event is not None, "test must set fake_stripe.next_event"
        return self.next_event


def _set_stripe_settings(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    settings = get_settings()
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture
def billing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stripe deliberately unconfigured — the app's shipped default.

    Pinned rather than assumed: a developer with real keys in ``.env`` would
    otherwise turn these into live-network tests.
    """
    _set_stripe_settings(
        monkeypatch,
        stripe_secret_key="",
        stripe_publishable_key="",
        stripe_webhook_secret="",
        stripe_price_pro_monthly="",
        stripe_price_pro_yearly="",
    )


@pytest.fixture
def fake_stripe(monkeypatch: pytest.MonkeyPatch) -> FakeStripe:
    """Billing fully configured, with the SDK swapped for :class:`FakeStripe`."""
    _set_stripe_settings(
        monkeypatch,
        stripe_secret_key="sk_test_fake",
        stripe_publishable_key="pk_test_fake",
        stripe_webhook_secret="whsec_fake",
        stripe_price_pro_monthly="price_fake_monthly",
        stripe_price_pro_yearly="price_fake_yearly",
    )
    fake = FakeStripe()
    monkeypatch.setattr(billing_service, "stripe", fake)
    return fake
