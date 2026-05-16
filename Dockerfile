# syntax=docker/dockerfile:1.7

# --- Builder ---
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# --- Runtime ---
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.12/site-packages"

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 app \
    && useradd  --system --uid 1001 --gid app --home /app --shell /usr/sbin/nologin app

COPY --from=builder /install /install

WORKDIR /app
COPY --chown=app:app . .

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default CMD = API server. Override in Cloud Run worker service with:
#   args: ["arq", "app.worker.WorkerSettings"]
CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker \
    -w ${WEB_CONCURRENCY:-1} \
    --bind 0.0.0.0:${PORT:-8000} \
    --timeout 120 \
    --graceful-timeout 30 \
    --access-logfile - \
    --log-level info"]
