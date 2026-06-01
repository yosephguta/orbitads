from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.core.database import create_db_and_tables
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.environment == "development":
        await create_db_and_tables()
    yield

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api.routes import auth, jobs, uploads, photos, listings, billing
app.include_router(auth.router,     prefix="/api/v1")
app.include_router(uploads.router,  prefix="/api/v1")
app.include_router(jobs.router,     prefix="/api/v1")
app.include_router(photos.router,   prefix="/api/v1")
app.include_router(listings.router, prefix="/api/v1")
app.include_router(billing.router,  prefix="/api/v1")

@app.get("/health", tags=["infra"])
async def health():
    return {"status": "ok", "version": settings.app_version}
