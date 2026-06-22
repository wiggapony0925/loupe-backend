#!/usr/bin/env python3
"""Provision Loupe Pro in Stripe via the SDK (no CLI auth needed).

Creates a "Loupe Pro" product + monthly/yearly prices using whatever
``STRIPE_SECRET_KEY`` is in the environment, and prints the env block to paste
into ``.env``. Use a **test** key (``sk_test_...``) unless you really mean live.

    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_provision.py

Idempotent-ish: it reuses an existing active "Loupe Pro" product if one exists,
so re-running won't pile up duplicates.
"""

from __future__ import annotations

import os
import sys

import stripe

PRODUCT_NAME = "Loupe Pro"
PRODUCT_DESC = (
    "Unlimited cards, scanner auto-import, full analytics, and "
    "tax/insurance statements."
)


def _find_product() -> stripe.Product | None:
    for p in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        if p.name == PRODUCT_NAME:
            return p
    return None


def _find_price(product_id: str, interval: str) -> stripe.Price | None:
    for pr in stripe.Price.list(
        product=product_id, active=True, limit=100
    ).auto_paging_iter():
        rec = pr.get("recurring") or {}
        if rec.get("interval") == interval and pr.currency == "usd":
            return pr
    return None


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("Set STRIPE_SECRET_KEY (use a sk_test_... key).", file=sys.stderr)
        return 1
    stripe.api_key = key
    live = key.startswith("sk_live")
    if live:
        print("⚠️  LIVE key detected — this creates real, sellable products.")
        if input("Type 'live' to continue: ").strip() != "live":
            print("Aborted.")
            return 1

    product = _find_product() or stripe.Product.create(
        name=PRODUCT_NAME, description=PRODUCT_DESC
    )
    monthly = _find_price(product.id, "month") or stripe.Price.create(
        product=product.id,
        currency="usd",
        unit_amount=999,
        recurring={"interval": "month"},
    )
    yearly = _find_price(product.id, "year") or stripe.Price.create(
        product=product.id,
        currency="usd",
        unit_amount=9900,
        recurring={"interval": "year"},
    )

    mode = "LIVE" if live else "TEST"
    print(f"\n✅ Done ({mode} mode). Paste into loupe-backend/.env:\n")
    print(f"STRIPE_PRICE_PRO_MONTHLY={monthly.id}")
    print(f"STRIPE_PRICE_PRO_YEARLY={yearly.id}")
    print("STRIPE_WEBHOOK_SECRET=whsec_...   # from `stripe listen`")
    print("BILLING_SUCCESS_URL=http://localhost:5173/app/settings?upgraded=1")
    print("BILLING_CANCEL_URL=http://localhost:5173/app/settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
