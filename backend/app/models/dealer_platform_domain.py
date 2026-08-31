from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class DealerPlatformDomain(SQLModel, table=True):
    """
    Maps a dealership domain to a shared DealerPlatform config.

    Many domains can point at ONE DealerPlatform row, so sites that use the same
    underlying template (dealer.com, CDK, etc.) don't each spawn a near-identical
    config. Approval (Part 5) writes a row here; GET /dealer-configs/domain/{domain}
    resolves via this table FIRST, then falls back to matching a DealerPlatform's
    source_url domain directly (so existing, un-migrated configs keep working
    without a backfill).
    """
    __tablename__ = "dealer_platform_domains"

    id:          Optional[int] = Field(default=None, primary_key=True)
    domain:      str           = Field(max_length=255, index=True, unique=True)
    platform_id: int           = Field(foreign_key="dealer_platforms.id", index=True)
    created_at:  datetime      = Field(default_factory=datetime.utcnow)
