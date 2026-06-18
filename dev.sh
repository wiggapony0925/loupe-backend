#!/usr/bin/env bash
# dev.sh — one-command local dev bootstrap for loupe-backend.
#
# Usage:
#   ./dev.sh          → start everything (Docker + backend)
#   ./dev.sh --down   → tear down Docker services (keeps volumes/data)
#
# What it does:
#   1. Ensures Docker Desktop is running (launches it if not)
#   2. Starts postgres + redis via docker compose (idempotent)
#   3. Waits until postgres is healthy (accepts connections)
#   4. Runs alembic migrations (safe to run repeatedly)
#   5. Starts the FastAPI dev server

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BACKEND_DIR/.venv"
COMPOSE="docker compose"

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[loupe]${NC} $*"; }
success() { echo -e "${GREEN}[loupe]${NC} $*"; }
warn()    { echo -e "${YELLOW}[loupe]${NC} $*"; }
error()   { echo -e "${RED}[loupe] ERROR:${NC} $*" >&2; }

# ── --down shortcut ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--down" ]]; then
    info "Stopping Docker services (data volumes preserved)…"
    cd "$BACKEND_DIR"
    $COMPOSE stop postgres redis
    success "Services stopped."
    exit 0
fi

# ── 1. Ensure Docker Desktop is running ───────────────────────────────────────
ensure_docker() {
    if docker info &>/dev/null; then
        return 0
    fi

    warn "Docker Desktop is not running — launching it now…"

    # Try to open Docker Desktop on macOS
    if [[ "$(uname)" == "Darwin" ]]; then
        open -a Docker 2>/dev/null || true
    fi

    local max_wait=60
    local waited=0
    while ! docker info &>/dev/null; do
        if (( waited >= max_wait )); then
            error "Docker Desktop didn't start after ${max_wait}s."
            error "Please open Docker Desktop manually and re-run: ./dev.sh"
            exit 1
        fi
        printf "  Waiting for Docker Desktop… (%ds)\r" "$waited"
        sleep 3
        (( waited += 3 ))
    done
    echo ""
    success "Docker Desktop is ready."
}

# ── 2. Start containers ───────────────────────────────────────────────────────
start_services() {
    info "Starting postgres + redis…"
    cd "$BACKEND_DIR"
    # Pull only if images are missing (avoids slow pull on every run)
    $COMPOSE up -d --no-recreate postgres redis 2>&1 | grep -v "^$" || true
    success "Containers started (or already running)."
}

# ── 3. Wait for Postgres to be healthy ────────────────────────────────────────
wait_for_postgres() {
    info "Waiting for Postgres to accept connections on localhost:5433…"
    local max_wait=60
    local waited=0

    while ! pg_isready -h localhost -p 5433 -q 2>/dev/null; do
        # Fallback: use docker exec if pg_isready isn't installed locally
        if docker compose exec -T postgres pg_isready -U loupe &>/dev/null 2>&1; then
            break
        fi
        if (( waited >= max_wait )); then
            error "Postgres didn't become ready after ${max_wait}s."
            error "Check Docker logs: docker compose logs postgres"
            exit 1
        fi
        printf "  Waiting for Postgres… (%ds)\r" "$waited"
        sleep 2
        (( waited += 2 ))
    done
    echo ""
    success "Postgres is ready."
}

# ── 4. Run migrations ─────────────────────────────────────────────────────────
run_migrations() {
    if [[ ! -f "$VENV/bin/alembic" ]]; then
        warn "Virtual environment not found — skipping migrations."
        warn "Run 'make install' first to set up the venv."
        return
    fi
    info "Running Alembic migrations…"
    cd "$BACKEND_DIR"
    "$VENV/bin/alembic" upgrade head
    success "Migrations applied."
}

# ── 5. Start the backend ──────────────────────────────────────────────────────
start_backend() {
    if [[ ! -f "$VENV/bin/python" ]]; then
        error "Virtual environment missing at $VENV"
        error "Run 'make install' to create it, then retry ./dev.sh"
        exit 1
    fi
    info "Starting FastAPI backend…"
    echo ""
    exec "$VENV/bin/python" "$BACKEND_DIR/run.py"
}

# ── Main ─────────────────────────────────────────────────────────────────────
ensure_docker
start_services
wait_for_postgres
run_migrations
start_backend
