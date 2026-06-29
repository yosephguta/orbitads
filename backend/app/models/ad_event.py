from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field, Column, Text
import sqlalchemy as sa


class AdEvent(SQLModel, table=True):
    __tablename__ = 'ad_events'

    id:             Optional[int] = Field(default=None, primary_key=True)
    user_id:        int           = Field(foreign_key='users.id', index=True)
    dealership_id:  Optional[int] = Field(default=None, foreign_key='dealerships.id', index=True)

    # Event type
    event_type: str = Field(max_length=50, index=True)
    # Values: 'generated' | 'generation_failed' | 'posted_marketplace' |
    #         'posted_fb_post' | 'posted_fb_groups' | 'sold_detected'

    # Links
    job_id:     Optional[int] = Field(default=None, foreign_key='jobs.id')
    listing_id: Optional[int] = Field(default=None, foreign_key='listings.id')

    # Vehicle info at time of event
    vehicle_year:          Optional[str] = Field(default=None, max_length=10)
    vehicle_make:          Optional[str] = Field(default=None, max_length=50)
    vehicle_model:         Optional[str] = Field(default=None, max_length=100)
    vehicle_price:         Optional[str] = Field(default=None, max_length=20)
    vehicle_price_range:   Optional[str] = Field(default=None, max_length=20)
    # budget(<15k) | mid(15-30k) | premium(30-50k) | luxury(50k+)
    vehicle_mileage_range: Optional[str] = Field(default=None, max_length=20)
    # low(<30k) | mid(30-80k) | high(80k+)

    # Generation details (populated for 'generated' and 'generation_failed')
    video_format:          Optional[str]  = Field(default=None, max_length=20)
    # 'listing' | 'slideshow' | 'with_outro'
    theme_used:            Optional[str]  = Field(default=None, max_length=50)
    voice_id_used:         Optional[str]  = Field(default=None, max_length=100)
    voice_type:            Optional[str]  = Field(default=None, max_length=20)
    # 'preloaded' | 'custom'
    custom_script_used:    Optional[bool] = Field(default=None)
    photos_selected_count: Optional[int]  = Field(default=None)
    render_time_seconds:   Optional[int]  = Field(default=None)
    failure_reason:        Optional[str]  = Field(default=None, sa_column=Column(Text))

    # Post details (populated for posting events)
    groups_count:                Optional[int]   = Field(default=None)
    time_since_generation_hours: Optional[float] = Field(default=None)

    # Timing
    created_at:  datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        index=True,
    )
    day_of_week: Optional[int] = Field(default=None)  # 0=Monday, 6=Sunday
    hour_of_day: Optional[int] = Field(default=None)  # 0-23
