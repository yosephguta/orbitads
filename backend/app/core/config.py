from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """
    All app configuration lives here.
    Values are read from the .env file automatically.
    If a value is missing from .env and has no default, the app
    will refuse to start — which is intentional. Better to crash
    early than to run with a missing API key.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── App ───────────────────────────────────────────────────
    app_name: str = "OrbitAds"
    app_version: str = "0.1.0"
    environment: str = "development"   # development | production
    debug: bool = True

    # ── Database ──────────────────────────────────────────────
    # Full connection string including username, password, host, db name
    # Format: postgresql+asyncpg://USER:PASSWORD@HOST:PORT/DBNAME
    database_url: str

    # ── Auth / JWT ────────────────────────────────────────────
    # Used to sign and verify login tokens
    # Generate with: openssl rand -hex 32
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24   # 24 hours

    # ── AWS ───────────────────────────────────────────────────
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"
    s3_bucket_name: str

    # ── AI Services ───────────────────────────────────────────
    anthropic_api_key: str
    elevenlabs_api_key: str = ""    # Phase 2 — blank for now
    heygen_api_key: str = ""        # Phase 2 — blank for now
    shotstack_api_key: str = ""     # Phase 3 — blank for now

    # ── Redis ─────────────────────────────────────────────────
    # Used for the job queue in Phase 2
    redis_url: str = "redis://localhost:6379/0"

    # ── CORS ──────────────────────────────────────────────────
    # Which frontend URLs are allowed to talk to this backend
    allowed_origins: list[str] = [
        "http://localhost:5173",      # React dev server
        "https://dealersorbit.com",   # production
    ]


@lru_cache
def get_settings() -> Settings:
    """
    Returns the Settings object.
    @lru_cache means it only reads the .env file once — not on every request.
    Import and call this anywhere you need a config value:

        from app.core.config import get_settings
        settings = get_settings()
        print(settings.app_name)
    """
    return Settings()