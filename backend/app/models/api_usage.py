from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class ApiUsage(SQLModel, table=True):
    """
    General paid-API usage log for cost attribution + the future admin
    dashboard. One row per significant API call (or batch). Keep it generic so
    scripts / captions / classifications / dealer-config all log here.

    `quantity` = units of work (e.g. number of photos classified — each photo
    is one Claude vision call; or 1 for a single script/caption call).
    `input_tokens`/`output_tokens` are optional — populated when the caller has
    them (e.g. dealer-config), left NULL otherwise.
    """
    __tablename__ = "api_usage"

    id: Optional[int] = Field(default=None, primary_key=True)
    call_type: str = Field(max_length=50, index=True)  # photo_classification | script | caption | dealer_config | ...
    user_id: Optional[int] = Field(default=None, index=True)
    quantity: int = Field(default=1)
    input_tokens: Optional[int] = Field(default=None)
    output_tokens: Optional[int] = Field(default=None)
    model: Optional[str] = Field(default=None, max_length=50)
    # Naive UTC — PostgreSQL TIMESTAMP WITHOUT TIME ZONE rejects aware datetimes (CLAUDE.md bug #24)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
