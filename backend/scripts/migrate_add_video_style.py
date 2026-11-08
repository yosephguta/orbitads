"""
Production migration (PostgreSQL / AWS RDS): add the five `users` video-style
columns used to customize the 9:16 generated videos:

    video_band_color       VARCHAR(20)  DEFAULT '#0d1f3c'            (top/bottom band bg)
    video_top_text_mode    VARCHAR(30)  NOT NULL DEFAULT 'hook_then_vehicle'
    video_top_custom_text  VARCHAR(200)
    video_cta_mode         VARCHAR(20)  NOT NULL DEFAULT 'phone'
    video_cta_custom_text  VARCHAR(200)

Existing rows inherit the defaults (dark-navy bands, hook-then-vehicle top text,
phone CTA — which falls back to DM at render time when the user has no phone).

Prod does NOT run SQLModel create_all (dev-only in lifespan). Idempotent —
ADD COLUMN IF NOT EXISTS.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_video_style
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_band_color VARCHAR(20) DEFAULT '#0d1f3c'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_top_text_mode VARCHAR(30) NOT NULL DEFAULT 'hook_then_vehicle'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_top_custom_text VARCHAR(200)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_cta_mode VARCHAR(20) NOT NULL DEFAULT 'phone'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_cta_custom_text VARCHAR(200)",
]


async def migrate():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        for stmt in STATEMENTS:
            await conn.execute(stmt)
            print("OK:", " ".join(stmt.split())[:80])
    finally:
        await conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
