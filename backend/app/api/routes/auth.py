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
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.models.user import User, UserCreate, UserRead, UserUpdate
from app.services.email import send_verification_email, send_welcome_email

router = APIRouter(prefix="/auth", tags=["auth"])


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

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email before signing in. Check your inbox for the verification link.",
        )

    token = create_access_token(user_id=user.id)
    return {"access_token": token, "token_type": "bearer"}


# ── Me ────────────────────────────────────────────────────────
@router.get("/me", response_model=UserRead)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
):
    return current_user


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, key, value)
    current_user.updated_at = datetime.now(timezone.utc)
    session.add(current_user)
    await session.commit()
    await session.refresh(current_user)
    return current_user
