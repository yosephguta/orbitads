"""
One-off production migration (PostgreSQL / AWS RDS).

Adds the three new `users` columns introduced across the Aug 2026 work:
  - last_extension_version    (extension version tracking)
  - password_reset_token      (password reset flow)
  - password_reset_expires_at (password reset flow)

Idempotent — uses ADD COLUMN IF NOT EXISTS, safe to re-run. Does NOT drop or
recreate anything. Run from the backend dir with the venv active:

    cd ~/orbitads/backend && source venv/bin/activate
    python -m scripts.migrate_add_user_columns

(No migration is needed for outro_videos.duration_seconds — that column
already exists in prod; this work only started populating it.)
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_extension_version VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(100)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_expires_at TIMESTAMP",
]


async def migrate():
    dsn = os.getenv("DATABASE_URL").replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    try:
        for stmt in STATEMENTS:
            await conn.execute(stmt)
            print(f"OK: {stmt}")
    finally:
        await conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
