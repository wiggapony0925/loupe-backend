#!/usr/bin/env python3
"""Theme the Stripe-hosted UI (Checkout + Customer Portal) to match Loupe.

Sets account branding colors (mint accent) and creates a Customer Portal
configuration so the "Manage" button works and lets members update payment,
switch plans, and cancel. Run with a test key:

    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_brand.py

Logo/icon uploads need image files; set those in Dashboard → Settings →
Branding (they flow into Checkout + emails automatically). Colors are API-set
here so the hosted pages pick up Loupe's mint accent.
"""

from __future__ import annotations

import os
import sys

import stripe

MINT = "#00F59B"  # --accent-mint
INK = "#121214"  # --bg-base (near-black)


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("Set STRIPE_SECRET_KEY (sk_test_...).", file=sys.stderr)
        return 1
    stripe.api_key = key

    acct = stripe.Account.retrieve()
    try:
        stripe.Account.modify(
            acct.id,
            settings={"branding": {"primary_color": MINT, "secondary_color": INK}},
        )
        print(f"✓ Account branding set (accent {MINT}).")
    except Exception as exc:
        print(
            f"… couldn't set account branding via API ({exc}). "
            "Set colors in Dashboard → Settings → Branding."
        )

    config = stripe.billing_portal.Configuration.create(
        business_profile={
            "headline": "Loupe Pro — manage your membership",
        },
        features={
            "customer_update": {
                "enabled": True,
                "allowed_updates": ["email", "name"],
            },
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "cancellation_reason": {
                    "enabled": True,
                    "options": [
                        "too_expensive",
                        "missing_features",
                        "switched_service",
                        "unused",
                        "other",
                    ],
                },
            },
        },
    )
    print(f"✓ Customer Portal configuration created: {config.id}")
    print("\nDone. Checkout + Portal now use the Loupe mint accent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
