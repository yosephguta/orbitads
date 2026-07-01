from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


class Dealership(SQLModel, table=True):
    __tablename__ = 'dealerships'

    id:               Optional[int] = Field(default=None, primary_key=True)
    dealer_group:     str           = Field(max_length=200)
    dealership_name:  str           = Field(max_length=200)
    location:         Optional[str] = Field(default=None, max_length=200)
    manager_user_id:  Optional[int] = Field(default=None)  # references users.id — no FK to avoid circular dep
    invite_code:      Optional[str] = Field(default=None, max_length=20, unique=True)
    required_tagline: Optional[str] = Field(default=None, max_length=200)
    required_tagline_es: Optional[str] = Field(default=None, max_length=200)
    created_at:       datetime      = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
