"""
Access dependency for dealership-owned resources (the inventory endpoints).

`require_dealership_member` gates on EFFECTIVE entitlement (resolve_entitlement,
so a salesperson inheriting the manager's active 'dealership' plan passes) and
requires the user to actually belong to a dealership. Routes using this MUST
scope every query by `current_user.dealership_id` — never accept a dealership_id
from the request (same hard invariant as the manager routes).
"""
from fastapi import Depends, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.services.entitlement import resolve_entitlement


async def require_dealership_member(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Allow only an active Dealership-plan member with a dealership_id.
    Returns the User on success; raises 403 DEALERSHIP_PLAN_REQUIRED otherwise.
    """
    ent = await resolve_entitlement(current_user, session)
    if (
        ent["status"] == "active"
        and ent["plan"] == "dealership"
        and current_user.dealership_id is not None
    ):
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "DEALERSHIP_PLAN_REQUIRED"},
    )
