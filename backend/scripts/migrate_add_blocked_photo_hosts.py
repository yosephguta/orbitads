"""
Production migration (PostgreSQL / AWS RDS): create the `blocked_photo_hosts`
table — photo-host domains whose CDN blocks Shotstack (so the pipeline proxies
their photos through S3 proactively).

Prod does NOT run SQLModel create_all (dev-only). Idempotent.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_blocked_photo_hosts
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS blocked_photo_hosts (
        id         SERIAL PRIMARY KEY,
        hostname   VARCHAR(255) UNIQUE NOT NULL,
        source     VARCHAR(50),
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_blocked_photo_hosts_hostname "
    "ON blocked_photo_hosts (hostname)",
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
