from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlmodel import Field, SQLModel


# ── Shared base ───────────────────────────────────────────────
class UserBase(SQLModel):
    email: str = Field(unique=True, index=True, max_length=255)
    full_name: str = Field(max_length=255)
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    dealership_name: str = Field(max_length=255, default="")
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    verification_token: Optional[str] = Field(default=None, max_length=255)
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    phone_number: Optional[str] = Field(default=None, max_length=20)
    outro_volume: float = Field(default=2.0)
    custom_tagline: Optional[str] = Field(default=None, max_length=200)
    preferred_language: str = Field(default='en', max_length=10)
    elevenlabs_voice_id_es: Optional[str] = Field(default='zDMHo7CPscBTgfDtPOWl', max_length=100)
    custom_tagline_es: Optional[str] = Field(default=None, max_length=200)

    # ── Multi-user / billing fields ───────────────────────────
    dealership_url: Optional[str] = Field(default=None, max_length=500)
    dealership_id: Optional[int] = Field(default=None, foreign_key='dealerships.id')
    role: str = Field(default='independent', max_length=50)  # independent | salesperson | manager | admin
    subscription_status: str = Field(default="trial", max_length=50)  # trial | active | cancelled | past_due
    signup_plan: str = Field(default='individual', max_length=20)  # individual | dealership
    purchased_plan: Optional[str] = Field(default=None, max_length=20)  # pro | elite | dealership | None (trial/unpaid)
    stripe_customer_id: Optional[str] = Field(default=None, max_length=255)
    stripe_subscription_id: Optional[str] = Field(default=None, max_length=255)
    trial_ends_at: Optional[datetime] = Field(default=None)
    terms_agreed_at: Optional[datetime] = Field(default=None)

    # ── Trial usage + quick launch URLs ───────────────────────
    trial_video_count: int = Field(default=0)
    cars_com_url: Optional[str] = Field(default=None, max_length=500)
    cargurus_url: Optional[str] = Field(default=None, max_length=500)
    dealer_inventory_url: Optional[str] = Field(default=None, max_length=500)
    dealer_config_requested: bool = Field(default=False)
    dealer_config_requested_at: Optional[datetime] = Field(default=None)

    # ── Extension version tracking (backward-compat visibility) ───
    last_extension_version: Optional[str] = Field(default=None, max_length=20)

    # ── Password reset ────────────────────────────────────────────
    password_reset_token: Optional[str] = Field(default=None, max_length=100)
    password_reset_expires_at: Optional[datetime] = Field(default=None)


# ── Database table ────────────────────────────────────────────
class User(UserBase, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: str
    created_at: datetime = Field(
        default_factory=datetime.utcnow
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow
    )


# ── API: Registration input ───────────────────────────────────
class UserCreate(SQLModel):
    first_name: str
    last_name: str
    email: str
    password: str
    phone_number: Optional[str] = None
    dealership_name: Optional[str] = None
    dealership_url: Optional[str] = None
    terms_agreed: bool = False
    signup_plan: str = 'individual'  # individual | dealership


# ── API: Response shape ───────────────────────────────────────
class UserRead(SQLModel):
    id: int
    email: str
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    dealership_name: str
    is_active: bool
    is_verified: bool
    dealership_id: Optional[int]
    role: str
    subscription_status: str
    signup_plan: str
    purchased_plan: Optional[str]
    elevenlabs_voice_id: Optional[str]
    phone_number: Optional[str]
    trial_ends_at: Optional[datetime]
    custom_tagline: Optional[str]
    preferred_language: str
    elevenlabs_voice_id_es: Optional[str]
    custom_tagline_es: Optional[str]
    dealership_url: Optional[str]
    terms_agreed_at: Optional[datetime]
    trial_video_count: int
    cars_com_url: Optional[str]
    cargurus_url: Optional[str]
    dealer_inventory_url: Optional[str]
    dealer_config_requested: bool
    dealer_config_requested_at: Optional[datetime]
    created_at: datetime


# ── API: Update shape ─────────────────────────────────────────
class UserUpdate(SQLModel):
    full_name: Optional[str] = None
    dealership_name: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    phone_number: Optional[str] = None
    custom_tagline: Optional[str] = None
    preferred_language: Optional[str] = None
    elevenlabs_voice_id_es: Optional[str] = None
    custom_tagline_es: Optional[str] = None
    dealership_url: Optional[str] = None
    cars_com_url: Optional[str] = None
    cargurus_url: Optional[str] = None
    dealer_inventory_url: Optional[str] = None
