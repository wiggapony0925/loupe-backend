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
if command -v nc >/dev/null 2>&1; then
    for i in {1..40}; do
        if nc -z "$PG_HOST" "$PG_PORT" 2>/dev/null; then
            break
        fi
        if [ "$i" -eq 1 ]; then
            echo "⏳ waiting for postgres at $PG_HOST:$PG_PORT…"
        fi
        sleep 0.5
    done
fi

echo "🚀 launching loupe-backend"
exec "$PY" run.py
