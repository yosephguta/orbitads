from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
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
    result = await session.exec(select(User).where(User.email == payload.email))
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    token = str(uuid.uuid4())
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        dealership_name=payload.dealership_name,
        hashed_password=hash_password(payload.password),
        role="salesperson",
        subscription_status="trial",
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=14),
        is_verified=False,
        verification_token=token,
        elevenlabs_voice_id="Gubgw9l4dtIoQA9YZHgx",  # Brian — default voice
    )

    session.add(user)
    await session.commit()
    await session.refresh(user)

    send_verification_email(to=user.email, full_name=user.full_name, token=token)

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
        return RedirectResponse("https://dealersorbit.com/orbitads/?verified=false")

    user.is_verified = True
    user.verification_token = None
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()

    send_welcome_email(to=user.email, full_name=user.full_name)

    return RedirectResponse("https://dealersorbit.com/orbitads/?verified=true")


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
