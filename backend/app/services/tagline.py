from __future__ import annotations

from typing import Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.models.dealership import Dealership


async def get_effective_tagline(session: AsyncSession, user: User) -> Optional[str]:
    """Dealership required_tagline takes priority over user custom_tagline."""
    if user.dealership_id:
        dealership = await session.get(Dealership, user.dealership_id)
        if dealership and dealership.required_tagline:
            return dealership.required_tagline

    return user.custom_tagline or None
