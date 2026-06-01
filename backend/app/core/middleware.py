from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.security import get_current_user
from app.models.user import User


def require_active_subscription(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """
    Dependency that blocks access if the user's subscription is expired.
    Use this on any route that requires an active account.

    Allowed states:
    - trial: within the 14-day trial window
    - active: paying subscriber

    Blocked states:
    - trial expired (trial_ends_at is in the past)
    - cancelled
    - past_due
    """
    now = datetime.now(timezone.utc)

    # Active paying subscriber — always allow
    if current_user.subscription_status == "active":
        return current_user

    # Trial — check if still within window
    if current_user.subscription_status == "trial":
        if current_user.trial_ends_at is None:
            # No trial end date set — allow but this shouldn't happen
            return current_user

        # Make trial_ends_at timezone-aware if it isn't
        trial_end = current_user.trial_ends_at
        if trial_end.tzinfo is None:
            trial_end = trial_end.replace(tzinfo=timezone.utc)

        if now <= trial_end:
            return current_user  # Still in trial
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "trial_expired",
                    "message": "Your 14-day free trial has ended. Please subscribe to continue.",
                    "upgrade_url": "https://dealersorbit.com/orbitads/#pricing",
                }
            )

    # Cancelled
    if current_user.subscription_status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "subscription_cancelled",
                "message": "Your subscription has been cancelled. Resubscribe to continue.",
                "upgrade_url": "https://dealersorbit.com/orbitads/#pricing",
            }
        )

    # Past due — payment failed
    if current_user.subscription_status == "past_due":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "payment_failed",
                "message": "Your last payment failed. Please update your payment method.",
                "portal_url": "https://api.dealersorbit.com/api/v1/billing/portal",
            }
        )

    # Catch-all for unknown statuses
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "subscription_required",
            "message": "An active subscription is required to use this feature.",
            "upgrade_url": "https://dealersorbit.com/orbitads/#pricing",
        }
    )


def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """
    Dependency that restricts access to admin users only.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return current_user