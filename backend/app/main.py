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

from app.models.ad_event import AdEvent  # noqa — ensures table is created
from app.models.api_usage import ApiUsage  # noqa — ensures table is created
from app.models.dealership import Dealership  # noqa — ensures table is created
from app.models.dealer_platform import DealerPlatform  # noqa — ensures table is created
from app.models.saved_script import SavedScript  # noqa — ensures table is created
from app.models.photo_classification_cache import PhotoClassificationCache  # noqa — ensures table is created
from app.api.routes import auth, jobs, uploads, photos, listings, billing, outros
from app.api.routes.dealer_configs import router as dealer_configs_router
from app.api.routes.saved_scripts import router as saved_scripts_router
from app.api.routes.admin import router as admin_router
from app.api.routes.manager import router as manager_router
app.include_router(auth.router,           prefix="/api/v1")
app.include_router(uploads.router,        prefix="/api/v1")
app.include_router(jobs.router,           prefix="/api/v1")
app.include_router(jobs.webhook_router,   prefix="/api/v1")  # no auth — called by Shotstack
app.include_router(photos.router,         prefix="/api/v1")
app.include_router(listings.router,       prefix="/api/v1")
app.include_router(billing.router,        prefix="/api/v1")
app.include_router(outros.router,         prefix="/api/v1")
app.include_router(dealer_configs_router, prefix="/api/v1")
app.include_router(saved_scripts_router,  prefix="/api/v1")
app.include_router(admin_router,          prefix="/api/v1")
app.include_router(manager_router,        prefix="/api/v1")

@app.get("/health", tags=["infra"])
async def health():
    return {"status": "ok", "version": settings.app_version}
