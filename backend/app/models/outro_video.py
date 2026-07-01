from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class OutroVideo(SQLModel, table=True):
    __tablename__ = "outro_videos"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=255)
    s3_key: str = Field(max_length=512)
    duration_seconds: Optional[float] = Field(default=None)
    created_at: datetime = Field(
        default_factory=datetime.utcnow
    )


class OutroVideoRead(SQLModel):
    id: int
    user_id: int
    name: str
    s3_key: str
    duration_seconds: Optional[float]
    created_at: datetime
    url: Optional[str] = None  # presigned URL, populated on read
