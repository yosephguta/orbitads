"""
Production migration (PostgreSQL / AWS RDS): add
`dealer_platforms.source_html_fragments` (JSON).

Stores the raw labeled HTML fragments the admin pasted into the Config Generator
(Part 4) so the /preview endpoint can re-run the generated selectors against the
original HTML without re-pasting. Also carries a `_request_user_id` key linking
the row to the requesting user (used by the Part 5 approval flow).

Prod does NOT run SQLModel create_all (dev-only in lifespan). Idempotent —
ADD COLUMN IF NOT EXISTS.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_source_html_fragments
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    "ALTER TABLE dealer_platforms ADD COLUMN IF NOT EXISTS source_html_fragments JSON",
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
