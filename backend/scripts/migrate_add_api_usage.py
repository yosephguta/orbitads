"""
Production migration (PostgreSQL / AWS RDS): create the `api_usage` table.

Prod does NOT run SQLModel create_all (dev-only in lifespan), so new tables
need an explicit CREATE. Idempotent — CREATE TABLE / INDEX IF NOT EXISTS.

    cd ~/orbitads/backend && source venv/bin/activate
    python -m scripts.migrate_add_api_usage
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS api_usage (
        id            SERIAL PRIMARY KEY,
        call_type     VARCHAR(50) NOT NULL,
        user_id       INTEGER,
        quantity      INTEGER NOT NULL DEFAULT 1,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        model         VARCHAR(50),
        created_at    TIMESTAMP NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_api_usage_call_type ON api_usage (call_type)",
    "CREATE INDEX IF NOT EXISTS ix_api_usage_created_at ON api_usage (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_api_usage_user_id ON api_usage (user_id)",
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
