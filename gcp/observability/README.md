# GCP observability config

Version-controlled Cloud Monitoring assets for the Loupe backend.
Applied via `gcloud monitoring dashboards create/update` and
`gcloud alpha monitoring policies create`. See `apply.sh` for the
exact commands.

## Files

- `dashboard.json` — single overview dashboard ("Loupe Backend
  Overview"): Cloud Run request rate / error rate / latency for
  `loupe-api`, instance counts for both services, Cloud Run Job
  success/failure counts (covers `loupe-migrate`, `loupe-seed`,
  `loupe-price-snapshot`), and a log-based panel counting circuit
  breaker opens.
- `policy-job-failure.json` — alert when ANY Cloud Run Job
  execution fails (catches missed `price_snapshot` runs).
- `policy-api-5xx.json` — alert when `loupe-api` 5xx rate exceeds
  1% over a 10-minute window.
- `policy-api-latency.json` — alert when `loupe-api` p95 latency
  exceeds 2 s over a 10-minute window.
- `log-metric-circuit-open.yaml` — log-based metric counting
  occurrences of "CircuitOpenError" in worker/api logs (feeds the
  dashboard's circuit-breaker panel).
- `apply.sh` — idempotent bootstrap script that creates the
  notification channel, dashboard, log metric, and all policies.
  Re-running it updates existing resources by display name.

## Notification channel

Email channel for `ninjeff06@gmail.com` is created on first run.
Swap to PagerDuty / Slack later by editing `apply.sh` and
re-running — `gcloud alpha monitoring channels create` is the only
line to change.
