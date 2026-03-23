from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TYPE_CHECKING

from sqlalchemy import Column, Text
from sqlmodel import Field, Relationship, SQLModel




# ── Status enum ───────────────────────────────────────────────
# An enum is a fixed set of allowed values for a field.
# This prevents typos like status="compelted" from slipping into the DB.
# The stages are in pipeline order — each phase adds more stages.
class JobStatus(str, Enum):
    PENDING           = "pending"             # job created, not started yet
    VIN_DECODING      = "vin_decoding"        # calling NHTSA API
    SCRIPT_GENERATING = "script_generating"   # calling Claude API
    VOICE_CLONING     = "voice_cloning"       # Phase 2 — ElevenLabs
    AVATAR_GENERATING = "avatar_generating"   # Phase 2 — HeyGen
    ASSEMBLING        = "assembling"           # Phase 3 — Shotstack
    COMPLETED         = "completed"            # video ready
    FAILED            = "failed"               # something went wrong


# ── Database table ────────────────────────────────────────────
class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Which user created this job
    user_id: int = Field(foreign_key="users.id", index=True)

    # ── Input fields ──────────────────────────────────────────
    # Either vin or listing_url must be provided — not necessarily both
    vin: Optional[str] = Field(default=None, max_length=17)
    listing_url: Optional[str] = Field(default=None, max_length=2048)

    # The creative direction for the ad — e.g. "family", "outdoorsy"
    theme: str = Field(max_length=100)

    # S3 keys for the salesperson's uploaded assets.
    # We store the S3 key (the file path in the bucket), not the full URL.
    # Full URLs are generated on demand using presigned URLs.
    #
    # photos_s3_keys stores multiple keys as a JSON string:
    # '["uploads/1/photos/abc.jpg", "uploads/1/photos/def.jpg"]'
    # We use Text (not VARCHAR) because the length is unpredictable.
    photos_s3_keys: Optional[str] = Field(
        default=None,
        sa_column=Column(Text)
    )
    voice_s3_key: Optional[str] = Field(default=None, max_length=512)

    # ── Pipeline output fields ────────────────────────────────
    # These start as None and get filled in as each stage completes.

    # JSON string of decoded vehicle data from NHTSA
    # e.g. '{"year": "2024", "make": "Kia", "model": "Telluride", ...}'
    vehicle_data: Optional[str] = Field(
        default=None,
        sa_column=Column(Text)
    )

    # JSON string of the Claude-generated script
    # e.g. '{"hook": "...", "body": "...", "cta": "...", "full_script": "..."}'
    generated_script: Optional[str] = Field(
        default=None,
        sa_column=Column(Text)
    )

    # Phase 2+ outputs — empty for now
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    heygen_video_url: Optional[str] = Field(default=None, max_length=2048)
    final_video_s3_key: Optional[str] = Field(default=None, max_length=512)
    final_video_url: Optional[str] = Field(default=None, max_length=2048)

    # ── Status tracking ───────────────────────────────────────
    status: JobStatus = Field(default=JobStatus.PENDING)

    # Human-readable error message if something goes wrong
    error_message: Optional[str] = Field(default=None, sa_column=Column(Text))

    # 0–100 percentage for the frontend progress bar
    progress_pct: int = Field(default=0)

    # ── Timestamps ────────────────────────────────────────────
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # Only set when status reaches "completed" or "failed"
    completed_at: Optional[datetime] = Field(default=None)

    # ── Relationship ──────────────────────────────────────────
    # Lets us do job.user to get the User who created this job
    user: Optional["User"] = Relationship(back_populates="jobs")


# ── API: Job creation input ───────────────────────────────────
# What the frontend sends when submitting a new ad request
class JobCreate(SQLModel):
    vin: Optional[str] = None
    listing_url: Optional[str] = None
    theme: str
    photos_s3_keys: Optional[str] = None   # JSON array string
    voice_s3_key: Optional[str] = None


# ── API: Job response ─────────────────────────────────────────
# What the API returns when the frontend polls for job status.
# Includes all the output fields so the frontend can show
# vehicle info, the script, and eventually the video.
class JobRead(SQLModel):
    id: int
    status: JobStatus
    progress_pct: int
    theme: str
    vin: Optional[str]
    vehicle_data: Optional[str]
    generated_script: Optional[str]
    final_video_url: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]