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
| `pytest -q` | Test suite (in-memory sqlite) |
| `alembic revision --autogenerate -m "msg"` | New migration |
| `alembic upgrade head` | Apply migrations |
| `docker compose up` | Local stack (postgres + redis + minio) |

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
