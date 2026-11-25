#!/usr/bin/env bash
# Prod deploy for DealersOrbit v2.0.0
# Run from EC2: ssh -i ~/.ssh/orbitads-key.pem ubuntu@50.19.170.160
#
# BEFORE running this script:
#   - The backend is already up to date on EC2 (pushed + pulled in the S3 session).
#   - No git pull or pip install needed; requirements.txt is unchanged.
#   - Run with --dry-run first; inspect output; then run again without it.
#
# Usage:
#   chmod +x prod_deploy_v2.0.0.sh
#   ./prod_deploy_v2.0.0.sh --dry-run    # preview all migrations, change nothing
#   ./prod_deploy_v2.0.0.sh              # apply all migrations, then restart

set -euo pipefail

PYTHON=/home/ubuntu/orbitads/venv/bin/python
BACKEND_DIR=/home/ubuntu/orbitads/backend

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

echo "=============================="
echo " DealersOrbit v2.0.0 prod deploy"
echo " dry_run=$DRY_RUN"
echo "=============================="
echo ""

cd "$BACKEND_DIR"

# ── Step 1: Dealership-tab schema migration ─────────────────────────────────
# Adds: dealership_inventory.{specs_json, specs_source, specs_captured_at, body_class},
#       users.highlights_language, inventory_highlights table + index.
# Idempotent (ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS).
echo ">> Step 1: migrate_step2_dealership_tab"
if $DRY_RUN; then
  $PYTHON -m scripts.migrate_step2_dealership_tab --dry-run
else
  $PYTHON -m scripts.migrate_step2_dealership_tab
fi
echo ""

# ── Step 2: JBA VDP price selector fix ─────────────────────────────────────
# Updates the detail_page.sale_price selector in the JBA Kia config so it
# resolves the final "JBA Price" on BOTH used and new-car VDPs (new cars
# returned NULL before this fix).
echo ">> Step 2: update_jba_vdp_price_selector"
if $DRY_RUN; then
  $PYTHON -m scripts.update_jba_vdp_price_selector
  echo "   (dry run — no change written)"
else
  $PYTHON -m scripts.update_jba_vdp_price_selector --apply
fi
echo ""

if $DRY_RUN; then
  echo "=============================="
  echo " Dry run complete. No changes made."
  echo " Re-run without --dry-run to apply."
  echo "=============================="
  exit 0
fi

# ── Step 3: Schema check + restart + health poll ────────────────────────────
# restart_and_check.sh: schema check → restart → poll /health every 2s for 40s.
# Exits 1 (without restarting) if schema check fails.
echo ">> Step 3: Schema check + restart + health check..."
bash "$BACKEND_DIR/scripts/restart_and_check.sh"
echo ""

echo "=============================="
echo " Deploy complete."
echo ""
echo " Verification (run from any machine with a valid token):"
echo ""
echo "   # S1: dealership schema — confirm columns exist"
echo "   # psql into RDS and run: \\d dealership_inventory"
echo "   # → should show body_class, specs_json, specs_source, specs_captured_at"
echo "   # psql: \\d inventory_highlights"
echo "   # → new table present"
echo "   # psql: \\d users | grep highlights_language"
echo "   # → highlights_language varchar(2)"
echo ""
echo "   # S2: JBA VDP price selector — from psql:"
echo "   SELECT config_json->'detail_page'->>'sale_price'"
echo "   FROM dealer_platforms"
echo "   WHERE id IN (SELECT platform_id FROM dealer_platform_domains WHERE domain='jbakia.com');"
echo "   # → combined used+new VDP selector"
echo ""
echo "   # S3: smoke test highlights endpoint (expect 403, indie-elite is not a Dealership member)"
echo "   curl -s -o /dev/null -w '%{http_code}' \\"
echo "     https://api.dealersorbit.com/api/v1/dealership/inventory/1/highlights \\"
echo "     -H 'Authorization: Bearer <token>'"
echo "   # → 403 or 404"
echo "=============================="
