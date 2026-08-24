from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class PhotoClassificationCache(SQLModel, table=True):
    """
    Cache of photo-URL → classification label so repeat imports (or re-scrapes
    by the sold-checker) don't pay to re-classify photos we've already seen.

    Keyed by `url_hash` (sha256 of the URL with query params stripped) so the
    same photo served with varying CDN query strings (?width=, cache-busters)
    hits the same cache row.
    """
    __tablename__ = "photo_classification_cache"

    id: Optional[int] = Field(default=None, primary_key=True)
    url_hash: str = Field(max_length=64, index=True, unique=True)
    photo_url: str = Field(max_length=1000)
    category: str = Field(max_length=20)
    # Naive UTC — PostgreSQL TIMESTAMP WITHOUT TIME ZONE rejects aware datetimes (CLAUDE.md bug #24)
    # Indexed so the >2-month auto-purge (DELETE WHERE created_at < cutoff) is efficient.
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


def hash_photo_url(url: str) -> str:
    """Hash the URL stripped of query params — CDN URLs vary by ?width= etc. but same photo."""
    base = url.split("?")[0]
    return hashlib.sha256(base.encode()).hexdigest()
