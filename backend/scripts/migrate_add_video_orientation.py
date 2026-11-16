"""
Production migration (PostgreSQL / AWS RDS) for Session 3 — horizontal (16:9) video.

Additive + idempotent. Safe to run twice (second run is a clean no-op).

  users.video_orientation  varchar(10) NOT NULL DEFAULT 'vertical'
  jobs.video_orientation   varchar(10) NOT NULL DEFAULT 'vertical'

Both NOT NULL + DEFAULT so existing rows backfill to 'vertical' automatically —
vertical stays the default; horizontal is opt-in per user and is stamped onto
each job at creation (POST /jobs).

Dev never ALTERs an existing table via create_all, so these ADD COLUMNs must run
here on dev too.

Usage:
    cd ~/orbitads/backend
    # preview only — prints the DDL, changes nothing:
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_video_orientation --dry-run
    # apply:
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_video_orientation
"""
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DDL_STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS video_orientation VARCHAR(10) NOT NULL DEFAULT 'vertical'",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS video_orientation VARCHAR(10) NOT NULL DEFAULT 'vertical'",
]


async def migrate(dry_run: bool):
    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        if dry_run:
            print("=== DRY RUN — no changes will be made ===\n")
            print("DDL that WOULD run:")
            for stmt in DDL_STATEMENTS:
                print("  " + " ".join(stmt.split()))
            for table in ("users", "jobs"):
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name=$1 AND column_name='video_orientation')",
                    table,
                )
                print(f"  {table}.video_orientation already present: {bool(exists)}")
            print("\n=== DRY RUN complete — nothing written ===")
            return

        # ── Apply ──────────────────────────────────────────────
        for stmt in DDL_STATEMENTS:
            await conn.execute(stmt)
            print("OK:", " ".join(stmt.split())[:72])
        print("Migration complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(migrate(dry))
