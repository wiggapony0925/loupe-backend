#!/usr/bin/env bash
set -e

PORT="${PORT:-8000}"

if command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti:"$PORT" || true)"
    if [ -n "$PIDS" ]; then
        echo "⚠️  killing existing process(es) on port $PORT: $PIDS"
        kill -9 $PIDS || true
    fi
fi

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3.12 || command -v python3)"
fi

# Wait for postgres on 5433 — asyncpg's SSL probe returns a cryptic
# "Connection reset by peer" if it races a still-booting container.
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5433}"
PG_WAIT_ATTEMPTS="${PG_WAIT_ATTEMPTS:-40}"
if command -v nc >/dev/null 2>&1; then
    PG_READY=0
    for ((i = 1; i <= PG_WAIT_ATTEMPTS; i++)); do
        if nc -z "$PG_HOST" "$PG_PORT" 2>/dev/null; then
            PG_READY=1
            break
        fi
        if [ "$i" -eq 1 ]; then
            echo "waiting for postgres at $PG_HOST:$PG_PORT..."
        fi
        sleep 0.5
    done
    if [ "$PG_READY" -ne 1 ]; then
        echo "postgres is not reachable at $PG_HOST:$PG_PORT"
        echo "start Docker Desktop and run: docker compose up -d postgres redis minio minio-init"
        exit 1
    fi
fi

echo "🚀 launching loupe-backend"
exec "$PY" run.py
