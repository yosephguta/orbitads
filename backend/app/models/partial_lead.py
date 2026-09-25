from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class PartialLead(SQLModel, table=True):
    __tablename__ = "partial_leads"

    id:              Optional[int] = Field(default=None, primary_key=True)
    first_name:      str           = Field(max_length=100)
    last_name:       str           = Field(max_length=100)
    email:           str           = Field(max_length=255, index=True)
    phone_number:    Optional[str] = Field(default=None, max_length=30)
    created_at:      datetime      = Field(default_factory=datetime.utcnow)
    lead_email_sent: bool          = Field(default=False)
