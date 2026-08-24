"""
Production migration (PostgreSQL / AWS RDS): create the
`photo_classification_cache` table.

Prod does NOT run SQLModel create_all (dev-only in lifespan), so new tables
need an explicit CREATE. Idempotent — CREATE TABLE / INDEX IF NOT EXISTS.

    cd ~/orbitads/backend && source venv/bin/activate
    python -m scripts.migrate_add_photo_classification_cache
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS photo_classification_cache (
        id         SERIAL PRIMARY KEY,
        url_hash   VARCHAR(64) NOT NULL UNIQUE,
        photo_url  VARCHAR(1000) NOT NULL,
        category   VARCHAR(20) NOT NULL,
        created_at TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_photo_classification_cache_url_hash "
    "ON photo_classification_cache (url_hash)",
    # Supports the >2-month auto-purge (DELETE WHERE created_at < cutoff).
    "CREATE INDEX IF NOT EXISTS ix_photo_classification_cache_created_at "
    "ON photo_classification_cache (created_at)",
]


async def migrate():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        for stmt in STATEMENTS:
            await conn.execute(stmt)
            print("OK:", " ".join(stmt.split())[:70])
    finally:
        await conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
