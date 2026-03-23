from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship, SQLModel

# TYPE_CHECKING is False at runtime — this import only runs for type hints.
# It lets us reference Job without causing a circular import.
if TYPE_CHECKING:
    from app.models.job import Job


# ── Shared base ───────────────────────────────────────────────
# These fields appear in the DB table AND in API responses.
# We put them in a base class so we don't repeat ourselves.
class UserBase(SQLModel):
    email: str = Field(unique=True, index=True, max_length=255)
    full_name: str = Field(max_length=255)
    dealership_name: str = Field(max_length=255, default="")
    is_active: bool = Field(default=True)


# ── Database table ────────────────────────────────────────────
# table=True tells SQLModel to create a real PostgreSQL table for this class.
# This is the class you use when reading/writing to the database.
class User(UserBase, table=True):
    __tablename__ = "users"

    # Primary key — the database assigns this automatically.
    # Optional[int] with default=None is the SQLModel pattern for auto-increment PKs.
    id: Optional[int] = Field(default=None, primary_key=True)

    # Never store the real password — only the bcrypt hash.
    hashed_password: str

    # Timestamps — set automatically using timezone-aware UTC.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # One user can have many jobs.
    # We'll use this later to load all ads a salesperson has created.
    jobs: list["Job"] = Relationship(back_populates="user")


# ── API: Registration input ───────────────────────────────────
# This is what the frontend sends when creating a new account.
# It includes the plain password — which we immediately hash and discard.
# Notice it does NOT inherit from UserBase — we define only what we need.
class UserCreate(SQLModel):
    email: str
    full_name: str
    dealership_name: str
    password: str   # plain text — gets hashed before storing


# ── API: Response shape ───────────────────────────────────────
# This is what the API sends back after registration or GET /me.
# It deliberately excludes hashed_password so it never leaks over the wire.
class UserRead(SQLModel):
    id: int
    email: str
    full_name: str
    dealership_name: str
    is_active: bool
    created_at: datetime