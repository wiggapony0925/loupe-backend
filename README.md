# Loupe Backend

FastAPI service powering the Loupe trading-card scanner ecosystem. It
authenticates users (Apple / Google), ingests multi-angle scan uploads to S3,
runs an async pipeline that grades + fingerprints the card, and streams progress
to the mobile app over WebSockets while syncing TCG catalog data from upstream
providers.

```
                ┌─────────────┐     ┌──────────────┐
   iOS / Web ──▶│  FastAPI    │────▶│  Postgres    │
                │  (app.*)    │     └──────────────┘
                │             │     ┌──────────────┐
                │             │────▶│  Redis       │
                │             │     └──────────────┘
                │             │     ┌──────────────┐
                │             │────▶│  S3 / R2     │
                │             │     └──────────────┘
                └──────┬──────┘
                       │  enqueue
                       ▼
                ┌─────────────┐     ┌──────────────┐
                │ arq workers │────▶│ scan pipeline│
                └─────────────┘     └──────────────┘
```

## Getting started

```bash
git clone <repo> loupe-backend && cd loupe-backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env  # then fill in secrets
alembic upgrade head
python run.py        # uvicorn on :8000
```

Worker:

```bash
arq app.worker.WorkerSettings
```

## Common commands

| Command | Purpose |
|---|---|
| `ruff check . --fix && ruff format .` | Lint + format |
| `mypy app` | Static types |
| `pytest -q` | Both test suites — see [Testing](#testing) |
| `pytest tests/database -q` | Postgres-backed schema + constraint suite |
| `alembic revision --autogenerate -m "msg"` | New migration |
| `alembic upgrade head` | Apply migrations |
| `docker compose up` | Local stack (postgres + redis + minio) |

## Testing

Two suites, two engines. Plain `pytest -q` runs both; so does CI.

| Suite | Engine | Needs |
|---|---|---|
| everything outside `tests/database` | in-memory SQLite | nothing running |
| `tests/database/` | a real Postgres 16 with `pgvector` | a live server |

The SQLite suite is fast and hermetic on purpose and stays that way. The
database suite covers what SQLite cannot answer at all: that the live schema
matches the models, that constraints really refuse bad rows, that cascades
cascade, that transactions isolate, and that the indexes the planner needs
exist.

### Running the database suite locally

```bash
docker compose up -d postgres      # pgvector/pgvector:pg16, published on :5433
.venv/bin/pytest tests/database -q
```

The harness defaults to `postgresql+asyncpg://loupe:loupe@localhost:5433/loupe`
— the port `docker-compose.yml` publishes. On that server it creates its own
scratch database (`loupe_dbtest_<pid>_<rand>`), builds the schema into it, and
drops it at the end; it never reads, writes or locks your `loupe` database.
Point it elsewhere with `LOUPE_TEST_PG_URL`:

```bash
LOUPE_TEST_PG_URL=postgresql+asyncpg://loupe:loupe@localhost:5432/loupe \
  .venv/bin/pytest tests/database -q
```

The image matters. Stock `postgres:16` does not ship the `vector` extension and
the harness creates one, so `docker-compose.override.yml` pins the pgvector
build (migration `0037_card_embeddings` needs it too).

With no server reachable, every test in the directory skips with a reason
naming the host it tried — so `pytest -q` still passes on a laptop with Docker
off.

### In CI

`.github/workflows/ci.yml` runs the SQLite suite (with `--ignore=tests/database`
so it is not collected twice), then runs the database suite against the
`pgvector/pgvector:pg16` service the job already provisions. Skipping is the
failure mode that looks like success, so the job writes a junit report and
fails the build when a database test skips instead of running.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `APP_ENV` | `dev` | One of `dev`/`test`/`prod` |
| `DATABASE_URL` | sqlite | Async SQLAlchemy DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Falls back to in-proc stub |
| `S3_ENDPOINT_URL` | — | Optional MinIO/R2 endpoint |
| `S3_BUCKET` | `loupe-uploads` | Bucket for scan artefacts |
| `JWT_PRIVATE_KEY_PEM` | ephemeral | RS256 signing key |
| `APPLE_CLIENT_ID`, `GOOGLE_CLIENT_ID` | — | OIDC audiences |

See `app/config.py` for the full list.

## Project layout

```
app/
  auth/        Apple + Google OIDC, JWT issuance
  clients/     httpx wrappers for upstream catalog APIs
  db/          SQLAlchemy base + session + alembic
  models/      ORM tables
  routers/     HTTP + WS endpoints
  schemas/     Pydantic request/response models
  services/    Business logic
  utils/       Logging, time, IDs
  workers/     arq tasks (scan pipeline, catalog sync)
documentation/ Markdown that builds the OpenAPI description
tests/         pytest suite (sqlite + ASGI)
```

## Deployment

`Dockerfile` builds the API container; `start.sh` runs `uvicorn` after applying
migrations. Workers run from the same image with `arq app.worker.WorkerSettings`.

## Card identification (OCR)

`POST /v1/cards/identify` accepts a multipart image upload and returns a
ranked list of catalog candidates plus an `identification_id` the client
uses to attach thumbs-up/down feedback (`POST /v1/cards/identify/{id}/feedback`).

### Provider selection

Set the `OCR_PROVIDER` env var:

| Value           | Behaviour                                                     |
| --------------- | ------------------------------------------------------------- |
| `mock`          | Default. Returns canned text for registered fixtures, empty otherwise. Zero cost, used by CI and the test suite. |
| `google_vision` | Real Google Cloud Vision (DOCUMENT_TEXT_DETECTION). Requires `GOOGLE_APPLICATION_CREDENTIALS` to point at a service-account JSON. |

```bash
export GOOGLE_APPLICATION_CREDENTIALS=secrets/gcp-sa.json
export OCR_PROVIDER=google_vision
```

Per-call cost (Vision Text Detection) is ~$0.0015 (first 1k/month free).
Each `CardIdentification` row records `cost_usd` so `GET
/v1/cards/admin/ocr/metrics?days=30` can report a running total.

### Feedback loop

When a user confirms or corrects a candidate, an `IdentificationFeedback`
row is persisted. The next identification with a similar parsed title
applies a popularity prior to candidates that recent users confirmed
correct (window: `OCR_FEEDBACK_BOOST_WINDOW_DAYS`, default 30). No model
training; the feedback acts purely as a re-rank boost so the loop is
safe to enable without retrain-on-write infra.

### Evaluation harness

`scripts/ocr_eval.py` downloads a curated fixture set (`tests/fixtures/ocr/fixtures.json`),
runs the full pipeline through whichever provider is configured, and prints
top-1 / top-3 accuracy, latency p50/p95, and estimated cost. A CSV
`ocr_eval_<provider>.csv` is written for spreadsheet drill-down.

```bash
make ocr-eval                         # uses the configured provider
make ocr-eval PROVIDER=google_vision  # one-off override
```
