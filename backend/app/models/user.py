from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlmodel import Field, SQLModel


# ── Shared base ───────────────────────────────────────────────
class UserBase(SQLModel):
    email: str = Field(unique=True, index=True, max_length=255)
    full_name: str = Field(max_length=255)
    dealership_name: str = Field(max_length=255, default="")
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    verification_token: Optional[str] = Field(default=None, max_length=255)
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    phone_number: Optional[str] = Field(default=None, max_length=20)
    outro_volume: float = Field(default=2.0)

    # ── Multi-user / billing fields ───────────────────────────
    dealership_id: Optional[int] = Field(default=None, foreign_key='dealerships.id')
    role: str = Field(default='independent', max_length=50)  # independent | salesperson | manager | admin
    subscription_status: str = Field(default="trial", max_length=50)  # trial | active | cancelled | past_due
    stripe_customer_id: Optional[str] = Field(default=None, max_length=255)
    stripe_subscription_id: Optional[str] = Field(default=None, max_length=255)
    trial_ends_at: Optional[datetime] = Field(default=None)


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
    is_verified: bool
    dealership_id: Optional[int]
    role: str
    subscription_status: str
    elevenlabs_voice_id: Optional[str]
    phone_number: Optional[str]
    trial_ends_at: Optional[datetime]
    created_at: datetime


# ── API: Update shape ─────────────────────────────────────────
class UserUpdate(SQLModel):
    full_name: Optional[str] = None
    dealership_name: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    phone_number: Optional[str] = None
