from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import SQLModel

from app.core.config import get_settings
from app.models.ad_event import AdEvent  # noqa
from app.models.dealership import Dealership  # noqa
from app.models.job import Job  # noqa
from app.models.listing import Listing  # noqa
from app.models.outro_video import OutroVideo  # noqa
from app.models.dealer_platform_domain import DealerPlatformDomain  # noqa
from app.models.blocked_photo_host import BlockedPhotoHost  # noqa

settings = get_settings()

# ── Engine ────────────────────────────────────────────────────
# The engine is created once when the app starts.
# It manages a pool of connections to PostgreSQL so the app
# doesn't have to open a new connection on every single request.
#
# pool_size=10     → keep up to 10 connections open at once
# max_overflow=20  → allow up to 20 extra connections under heavy load
# pool_pre_ping    → test each connection before using it (drops stale ones)
# echo=debug       → log every SQL statement in development (set to False in prod)
#
# timezone=UTC (connect_args): every connection runs with session TimeZone=UTC.
# The codebase's convention is naive-UTC datetimes at the DB boundary (bug #24).
# For `timestamp WITH time zone` columns (prod's ad_events/jobs from the June-2026
# SQLite→PG migration), Postgres casts a bound naive value USING the session
# TimeZone — so if the session weren't UTC (e.g. a dev Mac defaulting to
# America/New_York), a naive utcnow() would be stored at the wrong instant and
# read back shifted by the local offset (bug #56). Prod RDS already defaults to
# UTC; pinning it here makes that explicit and immune to OS/RDS-param drift, in
# both envs. asyncpg-only knob, so only pass it on Postgres.
_connect_args = (
    {"server_settings": {"timezone": "UTC"}}
    if "postgresql" in settings.database_url
    else {}
)
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,  # recycle connections every 5 min — prevents NAT/PG idle timeout drops
    connect_args=_connect_args,
)

# ── Session factory ───────────────────────────────────────────
# This is the factory that creates individual sessions (database conversations).
# expire_on_commit=False means objects stay usable after we commit —
# without this, accessing a field after saving would trigger another DB query.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Dependency ────────────────────────────────────────────────
# This is what routes will use to get a database session.
# FastAPI calls this function automatically when a route declares it.
# The `yield` means: give the route the session, then close it when done.
# Even if the route crashes, the session still gets closed — no leaks.
#
# Usage in a route:
#   async def my_route(session: AsyncSession = Depends(get_session)):
#       ...
async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


# ── Table creation helper ─────────────────────────────────────
# Creates all tables in the database based on our SQLModel models.
# We call this on startup in development so you don't have to run
# migrations manually while learning the project.
# In production, Alembic migrations handle this instead.
async def create_db_and_tables():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)