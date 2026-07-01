from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class JobStatus(str, Enum):
    PENDING           = "pending"
    VIN_DECODING      = "vin_decoding"
    SCRIPT_GENERATING = "script_generating"
    VOICE_CLONING     = "voice_cloning"
    ASSEMBLING        = "assembling"
    COMPLETED         = "completed"
    FAILED            = "failed"


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    # ── Input ─────────────────────────────────────────────────
    vin: Optional[str] = Field(default=None, max_length=17)
    listing_url: Optional[str] = Field(default=None, max_length=2048)
    theme: str = Field(max_length=100)
    custom_script: Optional[str] = Field(default=None, sa_column=Column(Text))

    # "slideshow" or "with_outro"
    video_type: str = Field(default="slideshow", max_length=50)

    # ID of the OutroVideo record to append (only used when video_type == "with_outro")
    outro_video_id: Optional[int] = Field(default=None, foreign_key="outro_videos.id")

    photos_s3_keys: Optional[str] = Field(default=None, sa_column=Column(Text))
    voice_s3_key: Optional[str] = Field(default=None, max_length=512)
    car_photo_urls: Optional[str] = Field(default=None, sa_column=Column(Text))
    price: Optional[str] = Field(default=None, max_length=50)
    language: Optional[str] = Field(default='en', max_length=10)

    # ── Pipeline outputs ──────────────────────────────────────
    vehicle_data: Optional[str] = Field(default=None, sa_column=Column(Text))
    generated_script: Optional[str] = Field(default=None, sa_column=Column(Text))
    final_video_s3_key: Optional[str] = Field(default=None, max_length=512)
    final_video_url: Optional[str] = Field(default=None, max_length=2048)

    # ── Shotstack ─────────────────────────────────────────────
    shotstack_render_id: Optional[str] = Field(default=None, max_length=100)

    # ── Status ────────────────────────────────────────────────
    status: JobStatus = Field(default=JobStatus.PENDING)
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text))
    progress_pct: int = Field(default=0)

    # ── Timestamps ────────────────────────────────────────────
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = Field(default=None)


class JobCreate(SQLModel):
    vin: Optional[str] = None
    listing_url: Optional[str] = None
    theme: str
    photos_s3_keys: Optional[str] = None
    voice_s3_key: Optional[str] = None
    car_photo_urls: Optional[str] = None
    video_type: str = "slideshow"
    outro_video_id: Optional[int] = None
    custom_script: Optional[str] = None
    price: Optional[str] = None
    language: Optional[str] = 'en'


class JobRead(SQLModel):
    id: int
    status: JobStatus
    progress_pct: int
    theme: str
    video_type: str
    outro_video_id: Optional[int]
    vin: Optional[str]
    vehicle_data: Optional[str]
    generated_script: Optional[str]
    final_video_url: Optional[str]
    error_message: Optional[str]
    language: Optional[str]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]
