from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlmodel import SQLModel

from app.core.config import get_settings

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
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
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