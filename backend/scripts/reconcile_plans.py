"""
Admin tool: set / reconcile specific users' plan (purchased_plan) and
subscription_status, targeted by email.

Two modes:

  reconcile  Pull each listed user's live plan from their own Stripe
             subscription and write it to the DB. Use for real individual
             subscribers whose stored plan drifted from Stripe (e.g. rows a
             one-off migration mis-stamped, or a plan switch that predated a
             working webhook).

  grant      Directly assign PLAN / STATUS to the listed emails — no Stripe
             lookup. Use to provision dealership salespeople who are covered
             under a dealership account and don't each have their own Stripe
             subscription.

Always scoped to the EMAILS list below (never all users). DRY_RUN previews
changes without writing.

Run from the backend dir with the venv active:

    cd ~/orbitads/backend && source venv/bin/activate
    python3 scripts/reconcile_plans.py
"""
import os
import sys
import asyncio

import asyncpg
import stripe
from dotenv import load_dotenv

# Make the app package importable no matter how this script is invoked
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings  # noqa: E402


# ── Config — edit these ───────────────────────────────────────
EMAILS = [
    # "salesperson1@somedealership.com",
    # "salesperson2@somedealership.com",
]

MODE = "grant"        # "grant" | "reconcile"

# grant mode only:
PLAN   = "dealership"  # "pro" | "elite" | "dealership"
STATUS = "active"      # "active" | "trial" | "past_due" | "cancelled"

DRY_RUN = True         # set False to actually write changes
# ──────────────────────────────────────────────────────────────

VALID_PLANS  = {"pro", "elite", "dealership"}
VALID_STATUS = {"active", "trial", "past_due", "cancelled"}


def _plan_from_subscription(sub, price_to_plan):
    """Resolve a plan name from a Stripe subscription's active price."""
    try:
        price_id = sub["items"]["data"][0]["price"]["id"]   # StripeObject: use [], not .get()
    except (KeyError, IndexError, TypeError):
        return None
    return price_to_plan.get(price_id)


def _status_from_subscription(sub):
    st = sub["status"]
    if st in ("active", "trialing"):                    return "active"
    if st == "past_due":                                return "past_due"
    if st in ("canceled", "unpaid", "incomplete_expired"): return "cancelled"
    return None  # unknown -> leave as-is


async def _grant(conn):
    if PLAN not in VALID_PLANS:
        raise SystemExit(f"PLAN must be one of {sorted(VALID_PLANS)}, got {PLAN!r}")
    if STATUS not in VALID_STATUS:
        raise SystemExit(f"STATUS must be one of {sorted(VALID_STATUS)}, got {STATUS!r}")

    print(f"GRANT plan={PLAN}, status={STATUS} to {len(EMAILS)} user(s). DRY_RUN={DRY_RUN}\n")
    changed = 0
    for email in EMAILS:
        row = await conn.fetchrow(
            "SELECT id, subscription_status, purchased_plan FROM users WHERE email=$1", email)
        if not row:
            print(f"  ! {email}: not found")
            continue
        if row["purchased_plan"] == PLAN and row["subscription_status"] == STATUS:
            print(f"  = {email}: already {PLAN}/{STATUS}")
            continue
        print(f"  ~ {email}: plan {row['purchased_plan']} -> {PLAN}, "
              f"status {row['subscription_status']} -> {STATUS}")
        changed += 1
        if not DRY_RUN:
            await conn.execute(
                "UPDATE users SET purchased_plan=$1, subscription_status=$2 WHERE id=$3",
                PLAN, STATUS, row["id"])
    return changed


async def _reconcile(conn):
    s = get_settings()
    stripe.api_key = s.stripe_secret_key
    price_to_plan = {
        s.stripe_price_pro:        "pro",
        s.stripe_price_elite:      "elite",
        s.stripe_price_dealership: "dealership",
    }
    price_to_plan.pop("", None)

    print(f"RECONCILE {len(EMAILS)} user(s) from Stripe. DRY_RUN={DRY_RUN}\n")
    changed = 0
    for email in EMAILS:
        row = await conn.fetchrow(
            "SELECT id, subscription_status, purchased_plan, stripe_subscription_id "
            "FROM users WHERE email=$1", email)
        if not row:
            print(f"  ! {email}: not found")
            continue
        if not row["stripe_subscription_id"]:
            print(f"  - {email}: no Stripe subscription — use grant mode for this user")
            continue
        try:
            sub = stripe.Subscription.retrieve(row["stripe_subscription_id"])
        except Exception as e:
            print(f"  ! {email}: could not fetch sub ({e})")
            continue

        plan = _plan_from_subscription(sub, price_to_plan)
        if plan is None:
            print(f"  ? {email}: subscription price not in Pro/Elite/Dealership map — skipping")
            continue
        new_status = _status_from_subscription(sub) or row["subscription_status"]

        if plan == row["purchased_plan"] and new_status == row["subscription_status"]:
            print(f"  = {email}: already {plan}/{new_status}")
            continue
        print(f"  ~ {email}: plan {row['purchased_plan']} -> {plan}, "
              f"status {row['subscription_status']} -> {new_status}")
        changed += 1
        if not DRY_RUN:
            await conn.execute(
                "UPDATE users SET purchased_plan=$1, subscription_status=$2 WHERE id=$3",
                plan, new_status, row["id"])
    return changed


async def run():
    if not EMAILS:
        raise SystemExit("EMAILS is empty — add the emails to target (this tool never touches all users).")
    if MODE not in ("grant", "reconcile"):
        raise SystemExit(f"MODE must be 'grant' or 'reconcile', got {MODE!r}")

    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        changed = await (_grant(conn) if MODE == "grant" else _reconcile(conn))
    finally:
        await conn.close()
    print(f"\n{'WOULD CHANGE' if DRY_RUN else 'CHANGED'} {changed} row(s).")


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(run())
