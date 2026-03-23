from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


# ── Shared base ───────────────────────────────────────────────
class UserBase(SQLModel):
    email: str = Field(unique=True, index=True, max_length=255)
    full_name: str = Field(max_length=255)
    dealership_name: str = Field(max_length=255, default="")
    is_active: bool = Field(default=True)
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    heygen_avatar_id: Optional[str] = Field(default=None, max_length=255)


# ── Database table ────────────────────────────────────────────
class User(UserBase, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── API: Registration input ───────────────────────────────────
class UserCreate(SQLModel):
    email: str
    full_name: str
    dealership_name: str
    password: str


# ── API: Response shape ───────────────────────────────────────
class UserRead(SQLModel):
    id: int
    email: str
    full_name: str
    dealership_name: str
    is_active: bool
    elevenlabs_voice_id: Optional[str]
    heygen_avatar_id: Optional[str]
    created_at: datetime


# ── API: Update shape ─────────────────────────────────────────
class UserUpdate(SQLModel):
    full_name: Optional[str] = None
    dealership_name: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    heygen_avatar_id: Optional[str] = None