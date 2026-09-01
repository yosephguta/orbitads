"""
Production migration (PostgreSQL / AWS RDS): add
`saved_scripts.content_type` (VARCHAR(20), NOT NULL DEFAULT 'video').

Distinguishes video-script prompts ('video') from caption/description prompts
('caption'). The DEFAULT 'video' correctly tags every existing row (all current
saved prompts are video-script prompts) with no data loss or reclassification.

Prod does NOT run SQLModel create_all (dev-only in lifespan). Idempotent —
ADD COLUMN IF NOT EXISTS.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_saved_script_content_type
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    "ALTER TABLE saved_scripts ADD COLUMN IF NOT EXISTS content_type VARCHAR(20) NOT NULL DEFAULT 'video'",
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
