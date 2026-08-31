"""
Production migration (PostgreSQL / AWS RDS): create the
`dealer_platform_domains` table.

Maps a dealership domain -> a shared DealerPlatform config (many domains, one
config). Used by GET /dealer-configs/domain/{domain} (resolves via this table
first) and written by the Part 5 approval flow.

Prod does NOT run SQLModel create_all (dev-only in lifespan), so new tables
need an explicit CREATE. Idempotent — CREATE TABLE / INDEX IF NOT EXISTS.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_dealer_platform_domains
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS dealer_platform_domains (
        id          SERIAL PRIMARY KEY,
        domain      VARCHAR(255) UNIQUE NOT NULL,
        platform_id INTEGER NOT NULL REFERENCES dealer_platforms(id),
        created_at  TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dealer_platform_domains_domain "
    "ON dealer_platform_domains (domain)",
    "CREATE INDEX IF NOT EXISTS idx_dealer_platform_domains_platform_id "
    "ON dealer_platform_domains (platform_id)",
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
