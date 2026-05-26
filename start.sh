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

echo "🚀 launching loupe-backend"
exec "$PY" run.py
