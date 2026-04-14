#!/usr/bin/env bash
# Start all three control-plane processes for local development.
# Press Ctrl+C once to shut everything down cleanly.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RESET='\033[0m'

log()  { echo -e "${GREEN}[start-dev]${RESET} $*"; }
warn() { echo -e "${YELLOW}[start-dev]${RESET} $*"; }

# ── cleanup on exit ───────────────────────────────────────────────────────────
PIDS=()
cleanup() {
    echo ""
    warn "Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    warn "Done."
}
trap cleanup INT TERM EXIT

# ── activate venv if present ──────────────────────────────────────────────────
if [[ -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# ── 1. Temporal dev server ────────────────────────────────────────────────────
log "Starting Temporal dev server  (gRPC :7233  UI :8080)…"
temporal server start-dev \
    --ip 0.0.0.0 \
    --port 7233 \
    --ui-port 8080 \
    --db-filename /tmp/temporal-dev.db \
    --namespace default \
    > /tmp/temporal-dev.log 2>&1 &
PIDS+=($!)

# Give Temporal a moment to be ready before the worker tries to connect.
sleep 3

# ── 2. Temporal worker ────────────────────────────────────────────────────────
log "Starting Temporal worker…"
python worker.py > /tmp/worker.log 2>&1 &
PIDS+=($!)

# ── 3. FastAPI (uvicorn) ──────────────────────────────────────────────────────
log "Starting FastAPI  (http://localhost:8000)…"
uvicorn main:app --reload --port 8000 &
PIDS+=($!)

log "All services up. Logs: /tmp/temporal-dev.log  /tmp/worker.log"
log "Press Ctrl+C to stop."

# Wait for any process to exit (e.g. uvicorn on file-change restart is fine;
# an unexpected crash surfaces here).
wait
