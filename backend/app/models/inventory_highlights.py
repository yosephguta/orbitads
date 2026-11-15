from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Column, Text
from sqlalchemy import UniqueConstraint
import sqlalchemy as sa


class InventoryHighlights(SQLModel, table=True):
    """
    Claude-generated salesperson talking points for one inventory vehicle, in one
    language. Cached per (inventory_id, language); regenerated only when the car's
    key data changes (tracked via input_hash) or the prompt version bumps.

    highlights_json is the JSON string of the structured response
    ({"highlights": [{title, detail, verify}], ...}). input_hash is the sha256
    over the fields fed to the prompt + language + PROMPT_VERSION (see
    app/services/highlights.py). All datetimes are naive UTC (bug #24).
    """
    __tablename__ = 'inventory_highlights'
    __table_args__ = (
        UniqueConstraint('inventory_id', 'language',
                         name='uq_inventory_highlights_inventory_language'),
    )

    id:            Optional[int] = Field(default=None, primary_key=True)
    inventory_id:  int           = Field(foreign_key='dealership_inventory.id', index=True)
    language:      str           = Field(max_length=2)  # 'en' | 'es'
    highlights_json: Optional[str] = Field(default=None, sa_column=Column(Text))
    input_hash:    str           = Field(max_length=64)
    model:         Optional[str] = Field(default=None, max_length=50)

    created_at:    datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )
    updated_at:    datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )
