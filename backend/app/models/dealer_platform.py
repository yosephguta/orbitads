from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Column, Text
import sqlalchemy as sa


class DealerPlatform(SQLModel, table=True):
    __tablename__ = 'dealer_platforms'

    id:            Optional[int] = Field(default=None, primary_key=True)
    name:          str           = Field(max_length=200)
    platform_slug: str           = Field(max_length=100, index=True)
    config_json:   dict          = Field(default={}, sa_column=Column(sa.JSON))
    status:        str           = Field(default='pending_review', max_length=20, index=True)
    # 'pending_review' | 'active' | 'rejected'
    source_url:    str           = Field(max_length=500)
    notes:         Optional[str] = Field(default=None, sa_column=Column(Text))

    generation_warnings: list        = Field(default=[], sa_column=Column(sa.JSON))
    input_tokens:        Optional[int] = Field(default=None)
    output_tokens:       Optional[int] = Field(default=None)

    reviewed_at: Optional[datetime] = Field(default=None)
    reviewed_by: Optional[str]      = Field(default=None, max_length=200)
    created_at:  datetime           = Field(
        default_factory=datetime.utcnow,
        index=True,
    )
