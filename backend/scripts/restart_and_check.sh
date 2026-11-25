#!/usr/bin/env bash
# Reusable prod restart guard for DealersOrbit backend.
#
# Steps:
#   1. check_schema (service venv, module form, from BACKEND_DIR)
#      → on failure: prints problems and exits WITHOUT restarting
#   2. sudo systemctl restart orbitads
#   3. poll HEALTH_URL every 2 s for up to 40 s until 200
#   4. on timeout: print last 50 service log lines and exit 1
#
# Usage:
#   bash scripts/restart_and_check.sh             # check + restart
#   bash scripts/restart_and_check.sh --dry-run   # check only (no restart)
#
# Local test (override health URL to point at dev server):
#   HEALTH_URL=http://localhost:8000/health bash scripts/restart_and_check.sh --dry-run
#
# Every future prod deploy script should end with:
#   bash "$BACKEND_DIR/scripts/restart_and_check.sh"

set -euo pipefail

PYTHON="${PYTHON:-/home/ubuntu/orbitads/venv/bin/python}"
BACKEND_DIR="${BACKEND_DIR:-/home/ubuntu/orbitads/backend}"
HEALTH_URL="${HEALTH_URL:-https://api.dealersorbit.com/health}"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# ── Step 1: schema check ────────────────────────────────────────────────────
echo ">> Checking schema (check_schema)..."
cd "$BACKEND_DIR"
if ! "$PYTHON" -m scripts.check_schema; then
  echo ""
  echo "SCHEMA CHECK FAILED — restart aborted."
  exit 1
fi

if $DRY_RUN; then
  echo ""
  echo "(--dry-run: restart skipped)"
  exit 0
fi

# ── Step 2: restart ─────────────────────────────────────────────────────────
echo ""
echo ">> Restarting orbitads service..."
sudo systemctl restart orbitads

# ── Step 3: health poll (20 × 2 s = 40 s max) ───────────────────────────────
echo ">> Polling $HEALTH_URL ..."
for i in $(seq 1 20); do
  sleep 2
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "FAIL")
  echo "   ($((i * 2))s) $HTTP"
  if [[ "$HTTP" == "200" ]]; then
    echo ""
    echo ">> Backend is up. Restart complete."
    exit 0
  fi
done

# ── Step 4: timeout ──────────────────────────────────────────────────────────
echo ""
echo "ERROR: health check timed out after 40 s. Dumping last 50 log lines:"
sudo journalctl -u orbitads -n 50 --no-pager
exit 1
