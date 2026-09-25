"""
Migration: create partial_leads table.

Run on dev first:
    cd orbitads/backend
    python3 scripts/migrate_add_partial_leads.py

Then run the same script on prod via EC2:
    /home/ubuntu/orbitads/venv/bin/python scripts/migrate_add_partial_leads.py
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
            CREATE TABLE IF NOT EXISTS partial_leads (
                id              SERIAL PRIMARY KEY,
                first_name      VARCHAR(100) NOT NULL,
                last_name       VARCHAR(100) NOT NULL,
                email           VARCHAR(255) NOT NULL,
                phone_number    VARCHAR(30),
                created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
                lead_email_sent BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_partial_leads_email
            ON partial_leads (email)
        """)
        print("Migration complete: partial_leads table ready.")
    finally:
        await conn.close()


asyncio.run(migrate())
