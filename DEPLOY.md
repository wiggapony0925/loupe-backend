# Deploying loupe-backend

This document covers the one-time GCP setup, the day-to-day deploy flow, and —
most importantly — **how the app connects to Postgres and Redis in every
environment**, so the `ConnectionResetError: [Errno 54] Connection reset by
peer` login outage cannot recur.

- **Project:** `loupe-app-56235`
- **Region:** `us-central1`
- **Cloud SQL instance:** `loupe-app-56235:us-central1:loupe-pg` (POSTGRES_16)
- **Cloud Run services:** `loupe-api`, `loupe-worker`
- **Cloud Run job:** `loupe-migrate` (Alembic migrations, runs before deploy)

---

## 1. Database connectivity — read this first

There are exactly **three** supported ways to reach Postgres. Anything else is
unsupported and will fail.

| # | Where | DSN shape | SSL |
|---|-------|-----------|-----|
| 1 | **Local Docker** (default dev) | `postgresql+asyncpg://loupe:loupe@localhost:5433/loupe` | no |
| 2 | **Cloud SQL unix socket** (production) | `postgresql+asyncpg://loupe:PASS@/loupe?host=/cloudsql/loupe-app-56235:us-central1:loupe-pg` | n/a (socket) |
| 3 | **Cloud SQL Auth Proxy** (local against the prod DB) | `postgresql+asyncpg://loupe:PASS@localhost:5432/loupe` | proxy-handled |

### Never use the Cloud SQL public IP

The instance has a public IP (`34.60.74.1`) with **no authorized networks** and
`requireSsl: false`. A direct TCP/SSL connection to it is reset mid-handshake,
which asyncpg surfaces as the cryptic:

```
ConnectionResetError: [Errno 54] Connection reset by peer
```

This is exactly the login crash that was observed. The app now **fails fast**:
`app/db/session.py` runs `_validate_database_url()` at engine-build time and
raises a clear `DatabaseConfigError` if `DATABASE_URL` points at a raw-IP
Postgres host while `CLOUD_SQL_CONNECTION_NAME` is set. See
`tests/platform/test_db_session.py` for the regression coverage.

### How production gets its DSN

`loupe-api` and `loupe-worker` read `DATABASE_URL` from the Secret Manager
secret **`database-url`** (the correct unix-socket DSN, method #2 above), and
have the Cloud SQL instance attached via the
`run.googleapis.com/cloudsql-instances` annotation. The `cloudrun-env.yaml`
file deliberately does **not** set `DATABASE_URL` — only
`CLOUD_SQL_CONNECTION_NAME` — so the secret is the single source of truth.

Verify any time with:

```bash
gcloud run services describe loupe-api \
  --region us-central1 --project loupe-app-56235 \
  --format='value(spec.template.metadata.annotations["run.googleapis.com/cloudsql-instances"])'

gcloud secrets versions access latest --secret=database-url --project loupe-app-56235
```

### Running locally against the prod DB (proxy)

```bash
# Install once: https://cloud.google.com/sql/docs/postgres/sql-proxy
cloud-sql-proxy loupe-app-56235:us-central1:loupe-pg --port 5432 &
export DATABASE_URL='postgresql+asyncpg://loupe:PASS@localhost:5432/loupe'
```

---

## 2. Redis connectivity

| Where | `REDIS_URL` | Notes |
|-------|-------------|-------|
| Local | `redis://localhost:6379/0` | docker-compose Redis |
| Production | a Memorystore IP over a VPC connector (`redis://10.x.x.x:6379/0`) | injected via the `redis-url` secret |

**Current production state (verify before relying on Redis):**
`loupe-api` currently has `REDIS_URL=redis://localhost:6379/0` and **no VPC
connector**, and there is **no Memorystore instance** provisioned. Redis is
therefore unreachable from Cloud Run. This is *not* fatal — the app degrades to
an in-process cache (`app/platform/redis_client.py`, 2s ping timeout →
`_InMemoryRedis`) — but it means cross-instance cache sharing and the arq queue
do not work in prod. To enable real Redis:

1. Create a Memorystore (Redis) instance in `us-central1`.
2. Create a Serverless VPC Access connector in the same VPC.
3. Attach the connector to `loupe-api` and `loupe-worker`
   (`--vpc-connector`), and set `REDIS_URL` (via the `redis-url` secret) to the
   Memorystore endpoint on both.

```bash
gcloud run services describe loupe-api \
  --region us-central1 --project loupe-app-56235 \
  --format='value(spec.template.metadata.annotations["run.googleapis.com/vpc-access-connector"])'
```

---

## 3. One-time setup (GitHub Actions → Cloud Run)

Run the idempotent helper. It creates the deployer service account, the
Workload Identity Federation pool/provider, and the IAM bindings:

```bash
./scripts/setup-gcp-deploy.sh
```

It prints two values to paste into the GitHub repo secrets
(Settings → Secrets and variables → Actions):

- `GCP_WIF_PROVIDER` — `projects/.../workloadIdentityPools/github-pool/providers/github`
- `GCP_DEPLOYER_SA` — `github-deployer@loupe-app-56235.iam.gserviceaccount.com`

The runtime secrets (`database-url`, `redis-url`, JWT keys, provider API keys)
live in Secret Manager and are referenced by the Cloud Run service config — not
created by the setup script.

---

## 4. Deploy flow

`.github/workflows/deploy.yml` runs automatically on every push to `main`
**after CI passes** (or via manual `workflow_dispatch`). Steps:

1. Build the image with Cloud Build, tagged with the 12-char commit SHA.
2. Update + execute the `loupe-migrate` job (Alembic) — runs **before** the
   services pick up new code, so new code never references unmigrated tables.
3. Deploy `loupe-api`, then `loupe-worker`.
4. Smoke-test `GET /health` (5 retries).
5. Move the `:latest` tag (non-fatal convenience step).

Rollback: deploy a previous SHA tag.

```bash
gcloud run deploy loupe-api \
  --image us-central1-docker.pkg.dev/loupe-app-56235/loupe/backend:<old-sha> \
  --region us-central1 --project loupe-app-56235
```

---

## 5. Local development

```bash
docker compose up -d            # Postgres:5433, Redis:6379, MinIO:9000/9001
cp .env.example .env            # defaults already point at the local stack
source .venv/bin/activate
python -m pytest -q             # tests use sqlite, no live services needed
uvicorn app.main:app --reload
```
