from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class BlockedPhotoHost(SQLModel, table=True):
    """
    Photo-host domains whose CDN blocks Shotstack's render servers (e.g.
    Cloudflare / dealereprocess → "access denied"). The pipeline proxies photos
    from these hosts through S3 proactively (no wasted first render).

    Populated two ways:
      - `runtime`: the pipeline hit an access-denied render and learned the host.
      - `config_gen`: config generation detected a Cloudflare-fronted photo host.
    Keyed by hostname because the photo CDN can differ from the dealer domain.
    """
    __tablename__ = "blocked_photo_hosts"

    id:         Optional[int] = Field(default=None, primary_key=True)
    hostname:   str           = Field(max_length=255, index=True, unique=True)
    source:     Optional[str] = Field(default=None, max_length=50)  # runtime | config_gen
    created_at: datetime      = Field(default_factory=datetime.utcnow)
