from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session

settings = get_settings()

# ── Password hashing ──────────────────────────────────────────
# CryptContext handles all the bcrypt complexity for us.
# "deprecated=auto" means old hash formats get upgraded automatically.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain_password: str) -> str:
    """
    Turn a plain password into a bcrypt hash for storing in the DB.
    Called once during registration.
    Example: hash_password("mypassword") → "$2b$12$xyz..."
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Check if a plain password matches a stored hash.
    Called during login.
    Example: verify_password("mypassword", "$2b$12$xyz...") → True
    """
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT ───────────────────────────────────────────────────────
def create_access_token(user_id: int) -> str:
    """
    Create a signed JWT token for a user.
    The token encodes:
      - sub: the user's ID (as a string)
      - exp: when the token expires

    The token is signed with SECRET_KEY so it can't be faked.
    Called after a successful login.
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {
        "sub": str(user_id),   # "subject" — who this token belongs to
        "exp": expire,          # expiry time
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> str:
    """
    Verify a JWT token and return the user ID inside it.
    Raises HTTP 401 if the token is invalid, expired, or tampered with.
    Called on every protected request.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm]
        )
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        return user_id
    except JWTError:
        raise credentials_exception


# ── Current user dependency ───────────────────────────────────
# This tells FastAPI where to find the token in incoming requests.
# It looks for:  Authorization: Bearer <token>
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    FastAPI dependency that returns the logged-in user.
    Drop this into any route that requires authentication.

    What it does:
      1. Extracts the Bearer token from the Authorization header
      2. Verifies the token signature and expiry
      3. Reads the user ID from inside the token
      4. Loads that user from the database
      5. Returns the user object to the route

    If anything fails, it raises HTTP 401 automatically.

    Usage in a route:
      async def my_route(current_user = Depends(get_current_user)):
          return current_user.email
    """
    # Import here to avoid circular imports
    # (User model will import from core, so we can't import User at the top)
    from app.models.user import User  # noqa: PLC0415

    user_id = decode_access_token(token)
    user = await session.get(User, int(user_id))

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )
    return user