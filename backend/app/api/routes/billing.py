from __future__ import annotations

import stripe
from typing import Annotated, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/billing", tags=["billing"])

# ── Price map ─────────────────────────────────────────────────
PRICE_MAP = {
    "pro":        settings.stripe_price_pro,
    "elite":      settings.stripe_price_elite,
    "dealership": settings.stripe_price_dealership,
}
# Reverse: Stripe price id -> plan name. Lets webhooks resolve the current plan
# from a subscription (e.g. after a Customer Portal plan switch).
PRICE_TO_PLAN = {v: k for k, v in PRICE_MAP.items() if v}


def _plan_from_subscription(subscription: dict):
    """Return 'pro'|'elite'|'dealership' from a Stripe subscription's active price, or None."""
    try:
        price_id = subscription["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    return PRICE_TO_PLAN.get(price_id)


# ── Create checkout session ───────────────────────────────────
@router.post("/checkout/{plan}")
async def create_checkout_session(
    plan: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: AsyncSession = Depends(get_session),
):
    """
    Creates a Stripe Checkout session for the given plan.
    Frontend redirects user to the returned url to complete payment.
    """
    if plan not in PRICE_MAP:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {plan}. Choose pro, elite, or dealership.")

    # Create or retrieve Stripe customer. Persist a newly-created customer id
    # immediately so an abandoned checkout doesn't spawn duplicate customers
    # on the next attempt.
    if not current_user.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=current_user.full_name,
            metadata={"user_id": current_user.id, "dealership": current_user.dealership_name},
        )
        customer_id = customer.id
        current_user.stripe_customer_id = customer_id
        session.add(current_user)
        await session.commit()
    else:
        customer_id = current_user.stripe_customer_id

    checkout = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{
            "price": PRICE_MAP[plan],
            "quantity": 1,
        }],
        mode="subscription",
        subscription_data={
            # No Stripe trial — users already got the 7-day / 5-video free trial
            # before subscribing, so charge immediately on checkout.
            "metadata": {"user_id": str(current_user.id), "plan": plan},
        },
        success_url="https://dealersorbit.com/?upgraded=true",
        cancel_url="https://dealersorbit.com/#pricing",
        metadata={"user_id": str(current_user.id), "plan": plan},
    )

    # `url` is the key the extension reads; keep checkout_url/session_id for back-compat.
    return {"url": checkout.url, "plan": plan, "checkout_url": checkout.url, "session_id": checkout.id}


# ── Customer portal ───────────────────────────────────────────
@router.post("/portal")
async def customer_portal(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Opens Stripe's customer portal so users can manage/cancel their subscription.
    """
    if not current_user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found.")

    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url="https://dealersorbit.com",
    )
    return {"url": session.url, "portal_url": session.url}


# ── Webhook ───────────────────────────────────────────────────
@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[Optional[str], Header(alias="stripe-signature")] = None,
    session: AsyncSession = Depends(get_session),
):
    """
    Stripe sends events here when subscriptions change.
    We update the user's subscription_status accordingly.
    """
    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.stripe_webhook_secret
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    # ── Handle events ─────────────────────────────────────────
    if event["type"] == "checkout.session.completed":
        await _handle_checkout_complete(event["data"]["object"], session)

    elif event["type"] == "customer.subscription.updated":
        await _handle_subscription_updated(event["data"]["object"], session)

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        await _handle_subscription_cancelled(event["data"]["object"], session)

    elif event["type"] == "invoice.payment_failed":
        await _handle_payment_failed(event["data"]["object"], session)

    return {"status": "ok"}


# ── Webhook helpers ───────────────────────────────────────────
async def _handle_checkout_complete(session_obj: dict, db):
    user_id = int(session_obj["metadata"]["user_id"])
    plan = session_obj["metadata"]["plan"]
    customer_id = session_obj["customer"]
    subscription_id = session_obj["subscription"]

    result = await db.exec(select(User).where(User.id == user_id))
    user = result.first()
    if not user:
        return

    user.stripe_customer_id = customer_id
    user.stripe_subscription_id = subscription_id
    user.subscription_status = "active"  # charged immediately, no Stripe trial
    user.purchased_plan = plan  # pro | elite | dealership
    db.add(user)
    await db.commit()


async def _handle_subscription_updated(subscription: dict, db):
    customer_id = subscription["customer"]
    status = subscription["status"]  # active | past_due | canceled | trialing

    result = await db.exec(select(User).where(User.stripe_customer_id == customer_id))
    user = result.first()
    if not user:
        return

    # User cancelled but chose "cancel at period end" — they keep access until
    # the period actually ends (customer.subscription.deleted fires then). Do NOT
    # revoke access now; Stripe still reports status "active" during this window.
    if subscription.get("cancel_at_period_end"):
        user.subscription_status = "active"
    else:
        user.subscription_status = "active" if status in ("active", "trialing") else status

    # Keep purchased_plan in sync with the subscription's current price. This is
    # what catches Customer Portal plan switches (upgrade/downgrade), which fire
    # customer.subscription.updated rather than checkout.session.completed.
    plan = _plan_from_subscription(subscription)
    if plan:
        user.purchased_plan = plan

    db.add(user)
    await db.commit()


async def _handle_subscription_cancelled(subscription: dict, db):
    customer_id = subscription["customer"]

    result = await db.exec(select(User).where(User.stripe_customer_id == customer_id))
    user = result.first()
    if not user:
        return

    user.subscription_status = "cancelled"
    db.add(user)
    await db.commit()


async def _handle_payment_failed(invoice: dict, db):
    customer_id = invoice["customer"]

    result = await db.exec(select(User).where(User.stripe_customer_id == customer_id))
    user = result.first()
    if not user:
        return

    user.subscription_status = "past_due"
    db.add(user)
    await db.commit()


# ── Contact Sales ─────────────────────────────────────────────
class ContactSalesRequest(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    dealership_name: Optional[str] = None
    message: Optional[str] = None


@router.post("/contact-sales")
async def contact_sales(payload: ContactSalesRequest):
    """Website 'Contact Sales' form — emails the sales team via Resend."""
    try:
        import resend
        resend.api_key = settings.resend_api_key

        resend.Emails.send({
            'from':    'DealersOrbit <notifications@mail.dealersorbit.com>',
            'to':      ['mail@dealersorbit.com'],
            'subject': f'📞 Contact Sales — {payload.dealership_name or payload.name}',
            'html':    f'''
                <h2>New Contact Sales Request</h2>
                <p><strong>Name:</strong> {payload.name}</p>
                <p><strong>Email:</strong> {payload.email}</p>
                <p><strong>Phone:</strong> {payload.phone or 'Not provided'}</p>
                <p><strong>Dealership:</strong> {payload.dealership_name or 'Not provided'}</p>
                <p><strong>Message:</strong> {payload.message or 'None'}</p>
            ''',
        })
        return {'success': True}
    except Exception as e:
        print(f'Contact sales email failed: {e}')
        raise HTTPException(status_code=500, detail='Failed to send message. Please try again.')
