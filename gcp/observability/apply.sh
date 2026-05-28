#!/usr/bin/env bash
# Idempotent Cloud Monitoring bootstrap for the Loupe backend.
#
# Usage:
#   ./gcp/observability/apply.sh                   # uses default email
#   ALERT_EMAIL=you@example.com ./apply.sh         # override
#
# Re-running is safe: dashboards / policies / channels are matched by
# display name and updated in place (or recreated for policies, since
# `gcloud alpha monitoring policies update` requires the resource id).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-loupe-app-56235}"
ALERT_EMAIL="${ALERT_EMAIL:-ninjeff06@gmail.com}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Project: ${PROJECT_ID}    Alert email: ${ALERT_EMAIL}"

# ---- 1. Notification channel ------------------------------------------------
CHANNEL_NAME="$(gcloud alpha monitoring channels list \
  --project="${PROJECT_ID}" \
  --filter="displayName='Loupe ops email' AND labels.email_address=${ALERT_EMAIL}" \
  --format='value(name)' | head -n1 || true)"

if [[ -z "${CHANNEL_NAME}" ]]; then
  echo "==> Creating notification channel for ${ALERT_EMAIL}"
  CHANNEL_NAME="$(gcloud alpha monitoring channels create \
    --project="${PROJECT_ID}" \
    --display-name='Loupe ops email' \
    --type=email \
    --channel-labels="email_address=${ALERT_EMAIL}" \
    --format='value(name)')"
else
  echo "==> Reusing notification channel: ${CHANNEL_NAME}"
fi

# ---- 2. Log-based metric ----------------------------------------------------
if gcloud logging metrics describe loupe_circuit_open \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> Updating log-based metric loupe_circuit_open"
  gcloud logging metrics update loupe_circuit_open \
    --project="${PROJECT_ID}" \
    --config-from-file="${HERE}/log-metric-circuit-open.yaml"
else
  echo "==> Creating log-based metric loupe_circuit_open"
  gcloud logging metrics create loupe_circuit_open \
    --project="${PROJECT_ID}" \
    --config-from-file="${HERE}/log-metric-circuit-open.yaml"
fi

# ---- 3. Dashboard -----------------------------------------------------------
DASH_NAME="$(gcloud monitoring dashboards list \
  --project="${PROJECT_ID}" \
  --filter="displayName='Loupe Backend Overview'" \
  --format='value(name)' | head -n1 || true)"

if [[ -z "${DASH_NAME}" ]]; then
  echo "==> Creating dashboard 'Loupe Backend Overview'"
  gcloud monitoring dashboards create \
    --project="${PROJECT_ID}" \
    --config-from-file="${HERE}/dashboard.json"
else
  echo "==> Updating dashboard ${DASH_NAME}"
  gcloud monitoring dashboards update "${DASH_NAME}" \
    --project="${PROJECT_ID}" \
    --config-from-file="${HERE}/dashboard.json"
fi

# ---- 4. Alert policies ------------------------------------------------------
apply_policy () {
  local file="$1" display_name="$2"
  local existing
  existing="$(gcloud alpha monitoring policies list \
    --project="${PROJECT_ID}" \
    --filter="displayName='${display_name}'" \
    --format='value(name)' | head -n1 || true)"
  if [[ -n "${existing}" ]]; then
    echo "==> Deleting + recreating policy '${display_name}' (${existing})"
    gcloud alpha monitoring policies delete "${existing}" \
      --project="${PROJECT_ID}" --quiet
  else
    echo "==> Creating policy '${display_name}'"
  fi
  gcloud alpha monitoring policies create \
    --project="${PROJECT_ID}" \
    --policy-from-file="${file}" \
    --notification-channels="${CHANNEL_NAME}"
}

apply_policy "${HERE}/policy-job-failure.json" "Cloud Run Job failure (any)"
apply_policy "${HERE}/policy-api-5xx.json"     "loupe-api 5xx rate > 1% (10m)"
apply_policy "${HERE}/policy-api-latency.json" "loupe-api p95 latency > 2s (10m)"

echo
echo "==> Done. Dashboard:"
echo "    https://console.cloud.google.com/monitoring/dashboards?project=${PROJECT_ID}"
echo "==> Alerting policies:"
echo "    https://console.cloud.google.com/monitoring/alerting?project=${PROJECT_ID}"
