from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import create_db_and_tables

settings = get_settings()


# ── Lifespan ──────────────────────────────────────────────────
# Code before `yield` runs on startup.
# Code after `yield` runs on shutdown.
# We use startup to create our database tables automatically.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # On startup: create tables if they don't exist yet.
    # In development this means you never have to run a migration manually.
    # In production we'll use Alembic migrations instead — but for now this
    # is the fastest way to get running.
    if settings.environment == "development":
        await create_db_and_tables()
    yield
    # On shutdown: nothing to clean up yet.


# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    # Only show the interactive Swagger docs in development.
    # In production /docs and /redoc return 404.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)


# ── CORS ──────────────────────────────────────────────────────
# Without this, the browser will block requests from the React frontend.
# allowed_origins comes from .env so it works in both dev and prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────
# We'll uncomment these one by one as we build each route file.
# They're listed here now so you can see the full picture.

from app.api.routes import auth, jobs
app.include_router(auth.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
# app.include_router(uploads.router, prefix="/api/v1")


# ── Health check ──────────────────────────────────────────────
# The simplest possible endpoint — just confirms the server is running.
# Your load balancer, uptime monitor, or your own curl command hits this.
@app.get("/health", tags=["infra"])
async def health():
    return {"status": "ok", "version": settings.app_version}


