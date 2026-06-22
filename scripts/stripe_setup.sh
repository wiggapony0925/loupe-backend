#!/usr/bin/env bash
#
# Provision Loupe Pro in Stripe (TEST MODE by default) and print the env block.
#
# Creates a "Loupe Pro" product + two recurring prices ($9.99/mo, $99/yr) and
# echoes the STRIPE_* env vars to paste into loupe-backend/.env. Runs against
# whatever account `stripe login` is authenticated as.
#
#   ./scripts/stripe_setup.sh           # test mode (safe — no real charges)
#   ./scripts/stripe_setup.sh --live    # LIVE mode (real, sellable products!)
#
# Then, in another terminal, forward webhooks to your local backend:
#   stripe listen --forward-to localhost:8000/v1/billing/webhook
# Copy the printed whsec_... into STRIPE_WEBHOOK_SECRET.
set -euo pipefail

LIVE=0
if [[ "${1:-}" == "--live" ]]; then
  LIVE=1
  echo "⚠️  LIVE MODE — this creates real, sellable products. Ctrl-C to abort."
  read -r -p "Type 'live' to continue: " confirm
  [[ "$confirm" == "live" ]] || { echo "Aborted."; exit 1; }
fi

command -v stripe >/dev/null 2>&1 || { echo "Stripe CLI not found. brew install stripe/stripe-cli/stripe"; exit 1; }

jqid() { python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'; }

# Single optional flag — kept as a scalar (not an array) so it's safe to expand
# under `set -u` on macOS's bash 3.2 when empty (test mode).
LIVE_FLAG=""
[[ $LIVE -eq 1 ]] && LIVE_FLAG="--live"

echo "Creating product…"
PRODUCT_ID=$(stripe products create $LIVE_FLAG \
  --name "Loupe Pro" \
  --description "Unlimited cards, scanner auto-import, full analytics, and tax/insurance statements." \
  | jqid)

echo "Creating monthly price (\$9.99)…"
PRICE_MONTHLY=$(stripe prices create $LIVE_FLAG \
  --product "$PRODUCT_ID" --currency usd --unit-amount 999 \
  -d "recurring[interval]=month" | jqid)

echo "Creating yearly price (\$99.00)…"
PRICE_YEARLY=$(stripe prices create $LIVE_FLAG \
  --product "$PRODUCT_ID" --currency usd --unit-amount 9900 \
  -d "recurring[interval]=year" | jqid)

cat <<EOF

✅ Done. Paste into loupe-backend/.env  ($([[ $LIVE -eq 1 ]] && echo LIVE || echo TEST) mode):

STRIPE_SECRET_KEY=sk_$([[ $LIVE -eq 1 ]] && echo live || echo test)_...   # from dashboard → Developers → API keys
STRIPE_PRICE_PRO_MONTHLY=$PRICE_MONTHLY
STRIPE_PRICE_PRO_YEARLY=$PRICE_YEARLY
STRIPE_WEBHOOK_SECRET=whsec_...   # from \`stripe listen\` (local) or the dashboard webhook (prod)
BILLING_SUCCESS_URL=http://localhost:5173/app/settings?upgraded=1
BILLING_CANCEL_URL=http://localhost:5173/app/settings

Next: stripe listen --forward-to localhost:8000/v1/billing/webhook
EOF
