from __future__ import annotations

"""
Effective entitlement resolution with DEALERSHIP-PLAN inheritance.

A salesperson under a dealership does NOT pay individually — they inherit access
from the dealership's MANAGER, but ONLY if the manager holds an active
'dealership' plan. If the manager is on any other plan (pro/elite), or is
cancelled/past_due/trial, the whole team has NO access (they must subscribe
individually — which decouples them from the dealership; see billing).

Managers and independent users use their own subscription_status/purchased_plan.

`resolve_entitlement` is the single source of truth — used by the
require_active_subscription middleware, /auth/me, and the create_job /
photos.classify gates so a dealership salesperson is treated consistently
everywhere. Effective plan 'dealership' unlocks everything Elite does
(outro, unlimited) and is shown in the extension as "Dealership".
"""

from sqlmodel import select

from app.models.user import User


async def resolve_entitlement(user, session) -> dict:
    """
    Returns {status, plan, source}:
      - source='dealership'          → inherited from an active-dealership manager (active / 'dealership')
      - source='dealership_inactive' → in a dealership but the dealership sub isn't active-dealership → blocked
      - source='own'                 → manager / independent — use the user's own status+plan
    """
    if getattr(user, "role", None) == "salesperson" and user.dealership_id:
        mgr = (
            await session.exec(
                select(User).where(
                    User.dealership_id == user.dealership_id,
                    User.role == "manager",
                )
            )
        ).first()
        if mgr and mgr.subscription_status == "active" and mgr.purchased_plan == "dealership":
            return {"status": "active", "plan": "dealership", "source": "dealership"}
        # In a dealership but no active dealership plan behind it → no access.
        return {"status": "cancelled", "plan": None, "source": "dealership_inactive"}

    return {
        "status": user.subscription_status,
        "plan": user.purchased_plan,
        "source": "own",
    }
