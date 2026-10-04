"""
Migration: add deleted_at column to outro_videos for soft-delete support.

The hard DELETE raised ForeignKeyViolationError when past jobs referenced the
outro via jobs.outro_video_id.  Soft-delete keeps the row (FK intact) and
filters it from all user-facing queries.

Run on dev first:
    cd orbitads/backend
    python3 scripts/migrate_add_outro_soft_delete.py

Then on prod via EC2:
    /home/ubuntu/orbitads/venv/bin/python scripts/migrate_add_outro_soft_delete.py
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def migrate():
    url = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("""
            ALTER TABLE outro_videos
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITHOUT TIME ZONE
        """)
        print("Migration complete: outro_videos.deleted_at column ready.")
    finally:
        await conn.close()


asyncio.run(migrate())
