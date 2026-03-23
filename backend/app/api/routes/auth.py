from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
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
from app.models.user import User, UserCreate, UserRead

# ── Router ────────────────────────────────────────────────────
# All routes in this file will be prefixed with /auth
# So register becomes /api/v1/auth/register (the /api/v1 comes from main.py)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Register ──────────────────────────────────────────────────
@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    """
    Create a new salesperson account.

    Steps:
    1. Check if email is already taken
    2. Hash the password
    3. Save the new user to the database
    4. Return the user (UserRead excludes the hashed password)
    """
    # Check for duplicate email
    # select(User).where(...) builds a SQL query: SELECT * FROM users WHERE email = ?
    result = await session.exec(select(User).where(User.email == payload.email))
    existing = result.first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    # Create the user — hash the password before storing
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        dealership_name=payload.dealership_name,
        hashed_password=hash_password(payload.password),
    )

    session.add(user)       # stage the insert
    await session.commit()  # write to database
    await session.refresh(user)  # reload from DB to get the auto-assigned id

    return user


# ── Login ─────────────────────────────────────────────────────
@router.post("/login")
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: Annotated[SQLModelAsyncSession, Depends(get_session)],
):
    """
    Log in with email and password. Returns a JWT token.

    OAuth2PasswordRequestForm expects form data with:
      username  (we use email here — standard OAuth2 calls it username)
      password

    The token returned looks like:
      {"access_token": "eyJhbGci...", "token_type": "bearer"}

    The frontend stores this and sends it with every future request:
      Authorization: Bearer eyJhbGci...
    """
    # Look up user by email
    result = await session.exec(select(User).where(User.email == form.username))
    user = result.first()

    # Check user exists AND password is correct
    # We do both checks together so we don't reveal whether the email exists
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

    # Create and return the JWT token
    token = create_access_token(user_id=user.id)
    return {"access_token": token, "token_type": "bearer"}


# ── Me ────────────────────────────────────────────────────────
@router.get("/me", response_model=UserRead)
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Return the currently logged-in user's profile.

    get_current_user (from security.py) handles everything:
    - Reads the token from the Authorization header
    - Verifies the signature
    - Loads the user from the database
    - Returns the user here, or raises 401 if anything is wrong

    The frontend calls this on page load to check if the session is still valid.
    """
    return current_user