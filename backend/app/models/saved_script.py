from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Column
import sqlalchemy as sa


class SavedScript(SQLModel, table=True):
    __tablename__ = 'saved_scripts'

    id:           Optional[int] = Field(default=None, primary_key=True)
    user_id:      int           = Field(foreign_key='users.id', index=True)
    name:         str           = Field(max_length=100)
    prompt_text:  str           = Field(sa_column=Column(sa.Text))
    use_count:    int           = Field(default=0)
    created_at:   datetime      = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = Field(default=None)
