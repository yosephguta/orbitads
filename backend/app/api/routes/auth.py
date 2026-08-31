from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelAsyncSession

from app.core.database import get_session
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.user import User, UserCreate, UserRead, UserUpdate
from app.models.dealership import Dealership
from app.services.s3 import upload_bytes, create_presigned_download_url, key_exists
from app.services.email import send_verification_email, send_welcome_email

router = APIRouter(prefix="/auth", tags=["auth"])

# Spanish voices not in users' ElevenLabs library by default.
# Preview clips are generated once via TTS and cached in S3.
_SPANISH_VOICE_IDS = {
    'zDMHo7CPscBTgfDtPOWl', 'G4IAP30yc6c1gK0csDfu',
    'k8cFOyAg7B9qwBlDDNTC', '9F4C8ztpNUmXkdDDbz3J',
    '8mBRP99B2Ng2QwsJMFQl', '22VndfJPBU7AZORAZZTT',
    'iqH5zmD4xxyGBHUsZ4Gt',
}
_PREVIEW_TEXT = '¡Hola! Estoy aquí para ayudarte a encontrar tu próximo vehículo.'
_voice_preview_cache: dict[str, str] = {}  # voice_id → presigned S3 URL


# ── Register ──────────────────────────────────────────────────
@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    if not payload.terms_agreed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must agree to the Terms of Service to create an account.",
        )

    result = await session.exec(select(User).where(User.email == payload.email))
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # Normalize dealership_url — strip protocol and trailing slash, store bare domain/path
    dealership_url = None
    if payload.dealership_url:
        dealership_url = (
            payload.dealership_url
            .strip()
            .rstrip('/')
            .replace('https://', '')
            .replace('http://', '')
            .replace('www.', '')
        )

    first_name = payload.first_name.strip()
    last_name = payload.last_name.strip()
    full_name = f'{first_name} {last_name}'.strip()

    signup_plan = payload.signup_plan if payload.signup_plan in ('individual', 'dealership') else 'individual'

    # Dealership-plan signups become managers (they'll own a Dealership + team
    # in later parts); everyone else is a salesperson. Only the dealership path
    # changes here — the individual path keeps the prior default.
    role = "manager" if signup_plan == "dealership" else "salesperson"

    token = str(uuid.uuid4())
    user = User(
        email=payload.email.lower().strip(),
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        dealership_name=payload.dealership_name or '',
        hashed_password=hash_password(payload.password),
        phone_number=payload.phone_number or None,
        role=role,
        subscription_status="trial",
        signup_plan=signup_plan,
        trial_ends_at=datetime.utcnow() + timedelta(days=7),
        is_verified=False,
        verification_token=token,
        elevenlabs_voice_id="Gubgw9l4dtIoQA9YZHgx",  # Brian — default voice
        elevenlabs_voice_id_es="zDMHo7CPscBTgfDtPOWl",  # Claus — default Spanish voice
        dealership_url=dealership_url,
        terms_agreed_at=datetime.utcnow(),
    )

    session.add(user)
    await session.commit()
    await session.refresh(user)

    send_verification_email(to=user.email, full_name=user.full_name, token=token)

    # Notify sales team of dealership-plan trial signups so they can reach out
    if signup_plan == 'dealership':
        try:
            import resend
            settings = get_settings()
            resend.api_key = settings.resend_api_key

            resend.Emails.send({
                'from':    'DealersOrbit <notifications@mail.dealersorbit.com>',
                'to':      ['mail@dealersorbit.com'],
                'subject': f'🏢 New Dealership Signup — {user.dealership_name or user.email}',
                'html':    f'''
                    <h2>New Dealership Plan Trial Signup</h2>
                    <p><strong>Name:</strong> {user.full_name}</p>
                    <p><strong>Email:</strong> {user.email}</p>
                    <p><strong>Phone:</strong> {user.phone_number or 'Not provided'}</p>
                    <p><strong>Dealership:</strong> {user.dealership_name or 'Not provided'}</p>
                    <p><strong>Dealership URL:</strong> {user.dealership_url or 'Not provided'}</p>
                    <p>They're on a 7-day trial (5 videos) with full Elite features.</p>
                    <p><strong>Action:</strong> Reach out to discuss multi-rooftop / team setup.</p>
                ''',
            })
        except Exception as e:
            print(f'Failed to send dealership signup notification: {e}')

    return {"message": "Account created! Please check your email to verify your account."}


# ── Verify email ───────────────────────────────────────────────
@router.get("/verify")
async def verify_email(
    token: Annotated[str, Query()],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    result = await session.exec(
        select(User).where(User.verification_token == token)
    )
    user = result.first()

    if not user:
        return RedirectResponse("https://dealersorbit.com/verified?status=invalid")

    user.is_verified = True
    user.verification_token = None
    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()

    send_welcome_email(to=user.email, full_name=user.full_name)

    return RedirectResponse("https://dealersorbit.com/verified")


# ── Login ─────────────────────────────────────────────────────
@router.post("/login")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    result = await session.exec(select(User).where(User.email == form.username))
    user = result.first()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )

    settings = get_settings()
    if not user.is_verified and not settings.skip_email_verification:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email before signing in. Check your inbox for the verification link.",
        )

    token = create_access_token(user_id=user.id)
    return {"access_token": token, "token_type": "bearer"}


# ── Password reset ────────────────────────────────────────────
class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    result = await session.exec(
        select(User).where(User.email == payload.email.lower().strip())
    )
    user = result.first()

    # Always return generic success — never reveal whether an email exists.
    generic_response = {
        "message": "If an account exists with that email, a reset link has been sent."
    }

    if not user:
        return generic_response

    # Naive UTC datetimes throughout — PostgreSQL TIMESTAMP WITHOUT TIME ZONE
    # rejects timezone-aware datetimes (see CLAUDE.md bug #24).
    token = secrets.token_urlsafe(32)
    user.password_reset_token = token
    user.password_reset_expires_at = datetime.utcnow() + timedelta(hours=1)
    session.add(user)
    await session.commit()

    try:
        import resend
        settings = get_settings()
        resend.api_key = settings.resend_api_key

        reset_url = f"https://dealersorbit.com/reset-password?token={token}"

        resend.Emails.send({
            "from": "DealersOrbit <notifications@mail.dealersorbit.com>",
            "to": [user.email],
            "subject": "Reset your DealersOrbit password",
            "html": f"""
                <div style="font-family:-apple-system,sans-serif;max-width:480px;margin:0 auto;padding:20px">
                  <h2>Reset your password</h2>
                  <p>Hi {user.first_name or user.full_name},</p>
                  <p>Click the button below to reset your DealersOrbit password. This link expires in 1 hour.</p>
                  <a href="{reset_url}" style="display:inline-block;background:#1a56db;color:white;
                     padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;margin:16px 0">
                    Reset Password
                  </a>
                  <p style="font-size:12px;color:#9ca3af">
                    If you didn't request this, you can safely ignore this email.
                  </p>
                </div>
            """,
        })
    except Exception as e:  # noqa: BLE001
        print(f"Failed to send reset email: {e}")

    return generic_response


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    result = await session.exec(
        select(User).where(User.password_reset_token == payload.token)
    )
    user = result.first()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link.")

    if (
        not user.password_reset_expires_at
        or user.password_reset_expires_at < datetime.utcnow()
    ):
        raise HTTPException(
            status_code=400,
            detail="This reset link has expired. Please request a new one.",
        )

    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters."
        )

    user.hashed_password = hash_password(payload.new_password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    session.add(user)
    await session.commit()

    return {"message": "Password reset successfully. You can now log in."}


# ── Support contact ───────────────────────────────────────────
class SupportRequest(BaseModel):
    subject: str
    message: str


@router.post("/support/contact")
async def contact_support(
    payload: SupportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    In-extension "Contact Support" form. Emails mail@dealersorbit.com with the
    user's context (plan, dealership, extension version) auto-attached so
    support has what it needs without a back-and-forth.
    """
    try:
        import resend
        settings = get_settings()
        resend.api_key = settings.resend_api_key

        resend.Emails.send({
            "from": "DealersOrbit <notifications@mail.dealersorbit.com>",
            "to": ["mail@dealersorbit.com"],
            "reply_to": current_user.email,
            "subject": f"🆘 Support Request — {payload.subject}",
            "html": f"""
                <h2>Support Request</h2>
                <p><strong>From:</strong> {current_user.full_name} ({current_user.email})</p>
                <p><strong>Dealership:</strong> {current_user.dealership_name or 'N/A'}</p>
                <p><strong>Plan:</strong> {current_user.purchased_plan or current_user.subscription_status}</p>
                <p><strong>Extension version:</strong> {current_user.last_extension_version or 'Unknown'}</p>
                <hr>
                <p><strong>Subject:</strong> {payload.subject}</p>
                <p><strong>Message:</strong></p>
                <p>{payload.message}</p>
            """,
        })
        return {"success": True}
    except Exception as e:  # noqa: BLE001
        print(f"Support email failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to send. Please email mail@dealersorbit.com directly.",
        )


# ── Me ────────────────────────────────────────────────────────
@router.get("/me")
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    dealership_required_tagline = None
    dealership_required_tagline_es = None
    if current_user.dealership_id:
        dealership = await session.get(Dealership, current_user.dealership_id)
        if dealership:
            dealership_required_tagline = dealership.required_tagline
            dealership_required_tagline_es = dealership.required_tagline_es

    settings = get_settings()
    now = datetime.now(timezone.utc)
    subscription_message = None
    is_blocked = False

    if current_user.subscription_status == "trial":
        if current_user.trial_ends_at is not None:
            trial_end = current_user.trial_ends_at
            if trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)
            # Dev bypass: never block the test account
            is_dev = bool(settings.dev_test_email and current_user.email == settings.dev_test_email)
            if not is_dev and now > trial_end:
                subscription_message = "trial_expired"
                is_blocked = True

    elif current_user.subscription_status == "past_due":
        updated = current_user.updated_at
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            grace_end = updated + timedelta(days=3)
        else:
            grace_end = now
        if now > grace_end:
            subscription_message = "past_due"
            is_blocked = True

    elif current_user.subscription_status in ("cancelled", "inactive"):
        subscription_message = "cancelled"
        is_blocked = True

    return {
        **UserRead.model_validate(current_user).model_dump(),
        "dealership_required_tagline": dealership_required_tagline,
        "dealership_required_tagline_es": dealership_required_tagline_es,
        "subscription_message": subscription_message,
        "is_blocked": is_blocked,
    }


@router.get("/voices/preloaded")
async def get_preloaded_voices(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Return preloaded voices with ElevenLabs preview URLs"""
    import httpx

    PRELOADED_VOICE_IDS = {
        # English
        'Gubgw9l4dtIoQA9YZHgx',  # Brian
        'onwK4e9ZLuTAKqWW03F9',  # Daniel
        'FGY2WhTYpPnrIDTdsKH5',  # Laura
        'OYTbf65OHHFELVut7v2H',  # Hope
        'pNInz6obpgDQGcFmaJgB',  # Adam
        'cjVigY5qzO86Huf0OWal',  # Eric
        'TX3LPaxmHKxFdv7VOQHJ',  # Liam
        'JBFqnCBsd6RMkjVDRZzb',  # George
        'IKne3meq5aSn9XLyUdCD',  # Charlie
        'bIHbv24MWmeRgasZH58o',  # Will
        'pqHfZKP75CvOlQylNhV4',  # Bill
        'iP95p4xoKVk53GoZ742B',  # Chris
        # Spanish
        'zDMHo7CPscBTgfDtPOWl',  # Claus
        'G4IAP30yc6c1gK0csDfu',  # Juan
        'k8cFOyAg7B9qwBlDDNTC',  # Miguel
        '9F4C8ztpNUmXkdDDbz3J',  # Dan
        '8mBRP99B2Ng2QwsJMFQl',  # El Faraon
        '22VndfJPBU7AZORAZZTT',  # Valeria
        'iqH5zmD4xxyGBHUsZ4Gt',  # Lis
    }

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                'https://api.elevenlabs.io/v1/voices',
                headers={'xi-api-key': settings.elevenlabs_api_key},
            )
            if not resp.is_success:
                raise Exception(f'ElevenLabs API error: {resp.status_code}')

            all_voices = resp.json().get('voices', [])
            preview_map = {
                v['voice_id']: v.get('preview_url')
                for v in all_voices
                if v['voice_id'] in PRELOADED_VOICE_IDS and v.get('preview_url')
            }

            # For Spanish voices not in the user's library, generate a TTS preview
            # clip once and cache it in S3 so every user gets previews on first load.
            missing_spanish = _SPANISH_VOICE_IDS - set(preview_map.keys())
            for voice_id in missing_spanish:
                # 1. In-memory cache (avoids presigned URL regeneration each request)
                if voice_id in _voice_preview_cache:
                    preview_map[voice_id] = _voice_preview_cache[voice_id]
                    continue

                s3_key = f'voice_previews/{voice_id}.mp3'

                # 2. Already generated and in S3
                if key_exists(s3_key):
                    url = create_presigned_download_url(s3_key, expires_in=604800)
                    _voice_preview_cache[voice_id] = url
                    preview_map[voice_id] = url
                    continue

                # 3. Generate TTS preview and upload to S3 (runs once per voice ever)
                try:
                    tts = await client.post(
                        f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
                        headers={
                            'xi-api-key':   settings.elevenlabs_api_key,
                            'Content-Type': 'application/json',
                        },
                        json={
                            'text':           _PREVIEW_TEXT,
                            'model_id':       'eleven_turbo_v2',
                            'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75},
                        },
                        timeout=30.0,
                    )
                    if tts.is_success:
                        upload_bytes(tts.content, s3_key, 'audio/mpeg')
                        url = create_presigned_download_url(s3_key, expires_in=604800)
                        _voice_preview_cache[voice_id] = url
                        preview_map[voice_id] = url
                        print(f'Generated voice preview for {voice_id}')
                except Exception as e:
                    print(f'Voice preview generation failed for {voice_id}: {e}')

            return {'preview_urls': preview_map}
    except Exception as e:
        print(f'Could not fetch ElevenLabs preview URLs: {e}')
        return {'preview_urls': {}}


@router.patch("/me")
async def update_me(
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(current_user, key, value)
    # Normalize empty string → None for optional text fields
    if updates.get("custom_tagline") == "":
        current_user.custom_tagline = None
    if updates.get("custom_tagline_es") == "":
        current_user.custom_tagline_es = None
    # Quick launch URLs — strip whitespace, empty → None
    if payload.cars_com_url is not None:
        current_user.cars_com_url = payload.cars_com_url.strip() or None
    if payload.cargurus_url is not None:
        current_user.cargurus_url = payload.cargurus_url.strip() or None
    if payload.dealer_inventory_url is not None:
        # Strip campaign query params / fragments (utm_*, gclid, gbraid, etc.) —
        # a pasted landing/ppc link like ".../?utm_source=google&gclid=..." should
        # be stored as the clean site URL for config generation + domain mapping.
        _inv = payload.dealer_inventory_url.strip().split("#")[0].split("?")[0].strip()
        current_user.dealer_inventory_url = _inv or None
    # Validate preferred_language
    if "preferred_language" in updates:
        if updates["preferred_language"] not in ("en", "es"):
            current_user.preferred_language = "en"
    current_user.updated_at = datetime.utcnow()
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)

    dealership_required_tagline = None
    dealership_required_tagline_es = None
    if current_user.dealership_id:
        dealership = await session.get(Dealership, current_user.dealership_id)
        if dealership:
            dealership_required_tagline = dealership.required_tagline
            dealership_required_tagline_es = dealership.required_tagline_es

    return {
        **UserRead.model_validate(current_user).model_dump(),
        "dealership_required_tagline": dealership_required_tagline,
        "dealership_required_tagline_es": dealership_required_tagline_es,
    }


# ── Request dealer site configuration ─────────────────────────
@router.post("/request-dealer-config")
async def request_dealer_config(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    """Paid user requests manual dealer site configuration."""

    # Must be on a paid plan
    if current_user.subscription_status not in ("active",):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dealer site configuration is available on paid plans.",
        )

    if not current_user.dealer_inventory_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please save your dealer inventory URL first.",
        )

    # Mark as requested
    current_user.dealer_config_requested = True
    current_user.dealer_config_requested_at = datetime.utcnow()
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)

    # Send email notification via Resend
    try:
        import resend
        settings = get_settings()
        resend.api_key = settings.resend_api_key

        resend.Emails.send({
            'from':    'DealersOrbit <notifications@mail.dealersorbit.com>',
            'to':      ['mail@dealersorbit.com'],
            'subject': f'Dealer Config Request — {current_user.dealership_name or current_user.email}',
            'html':    f'''
                <h2>New Dealer Site Configuration Request</h2>
                <p><strong>User:</strong> {current_user.full_name} ({current_user.email})</p>
                <p><strong>Dealership:</strong> {current_user.dealership_name or "Not set"}</p>
                <p><strong>Inventory URL:</strong> <a href="{current_user.dealer_inventory_url}">{current_user.dealer_inventory_url}</a></p>
                <p><strong>Subscription:</strong> {current_user.subscription_status}</p>
                <p><strong>Requested at:</strong> {current_user.dealer_config_requested_at}</p>
                <hr>
                <p>Log in to approve: <a href="https://api.dealersorbit.com/docs">Admin panel</a></p>
            ''',
        })
    except Exception as e:
        print(f'Failed to send config request email: {e}')

    return {
        'message': 'Configuration request submitted. Your dealer site will be configured within 24 hours.',
        'requested_at': current_user.dealer_config_requested_at,
    }
