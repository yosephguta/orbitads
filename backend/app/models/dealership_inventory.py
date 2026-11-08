from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field, Column, Text
from sqlalchemy import UniqueConstraint, Index
import sqlalchemy as sa


class DealershipInventory(SQLModel, table=True):
    """
    A single vehicle in a dealership's owned inventory.

    Dealership-scoped (not user-scoped): any member of the dealership with an
    effective Dealership plan may sync/read it. Rows arrive from the extension's
    "Add this page to inventory" button today (source='extension_sync') and from
    real feeds later (source='feed:homenet', etc.). Upsert key is
    (dealership_id, vin); partial page syncs never mark cars 'removed'.

    All datetime columns are naive UTC (bug #24): default_factory=datetime.utcnow
    on the Python side, plus a matching server_default so a raw INSERT (feed
    importer) also gets a value. removed_at stays NULL until a row is removed.
    """
    __tablename__ = 'dealership_inventory'
    __table_args__ = (
        UniqueConstraint('dealership_id', 'vin',
                         name='uq_dealership_inventory_dealership_vin'),
        Index('ix_dealership_inventory_dealership_stock',
              'dealership_id', 'stock_number'),
        Index('ix_dealership_inventory_dealership_status',
              'dealership_id', 'status'),
    )

    id:             Optional[int] = Field(default=None, primary_key=True)
    dealership_id:  int           = Field(foreign_key='dealerships.id', index=True)
    vin:            str           = Field(max_length=17)  # stored uppercase

    stock_number:   Optional[str] = Field(default=None, max_length=50)
    condition:      Optional[str] = Field(default=None, max_length=10)  # 'new' | 'used' | None

    year:           Optional[int] = Field(default=None)
    make:           Optional[str] = Field(default=None, max_length=100)
    model:          Optional[str] = Field(default=None, max_length=100)
    trim:           Optional[str] = Field(default=None, max_length=100)

    # Filled from NHTSA decode (background task)
    body_style:     Optional[str] = Field(default=None, max_length=100)
    drivetrain:     Optional[str] = Field(default=None, max_length=100)
    fuel_type:      Optional[str] = Field(default=None, max_length=100)
    engine:         Optional[str] = Field(default=None, max_length=100)

    exterior_color: Optional[str] = Field(default=None, max_length=50)
    interior_color: Optional[str] = Field(default=None, max_length=50)

    mileage:        Optional[int] = Field(default=None)  # whole miles
    price:          Optional[int] = Field(default=None)  # whole dollars, final advertised price

    listing_url:    Optional[str] = Field(default=None, max_length=2048)  # query string + fragment stripped (bug #65)
    photo_urls:     Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON array string (listings convention)
    carfax_url:     Optional[str] = Field(default=None, max_length=2048)
    vehicle_data:   Optional[str] = Field(default=None, sa_column=Column(Text))  # NHTSA decode JSON string

    source:         str           = Field(
        default='extension_sync', max_length=30,
        sa_column_kwargs={'server_default': 'extension_sync'},
    )  # later 'feed:homenet' etc.
    status:         str           = Field(
        default='active', max_length=20,
        sa_column_kwargs={'server_default': 'active'},
    )  # 'active' | 'removed'

    synced_by_user_id: Optional[int] = Field(default=None, foreign_key='users.id')

    first_seen_at:  datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )
    last_seen_at:   datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )
    removed_at:     Optional[datetime] = Field(default=None)
    created_at:     datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )
    updated_at:     datetime = Field(
        default_factory=datetime.utcnow,
        sa_column_kwargs={'server_default': sa.text('now()')},
    )


# ── API: Read shape ───────────────────────────────────────────
class DealershipInventoryRead(SQLModel):
    id: int
    dealership_id: int
    vin: str
    stock_number: Optional[str]
    condition: Optional[str]
    year: Optional[int]
    make: Optional[str]
    model: Optional[str]
    trim: Optional[str]
    body_style: Optional[str]
    drivetrain: Optional[str]
    fuel_type: Optional[str]
    engine: Optional[str]
    exterior_color: Optional[str]
    interior_color: Optional[str]
    mileage: Optional[int]
    price: Optional[int]
    listing_url: Optional[str]
    photo_urls: Optional[str]
    carfax_url: Optional[str]
    vehicle_data: Optional[str]
    source: str
    status: str
    synced_by_user_id: Optional[int]
    first_seen_at: datetime
    last_seen_at: datetime
    removed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
