"""
Production migration (PostgreSQL / AWS RDS): create the
`dealership_inventory` table.

Dealership-owned vehicle inventory, synced from the extension today and from
real feeds later. Upsert key is (dealership_id, vin). See
app/models/dealership_inventory.py — this DDL is built to match exactly what
SQLModel create_all() generates for that model (verified via a scratch schema),
so dev (create_all) and prod (this migration) produce an identical table.

Prod does NOT run create_all (dev-only in lifespan), so new tables need an
explicit CREATE. Idempotent — CREATE TABLE / INDEX IF NOT EXISTS (the inline
PK / FK / UNIQUE constraints ride along with the CREATE TABLE, so they are not
re-added on a second run).

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_add_dealership_inventory
"""
import asyncio
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS dealership_inventory (
        id                SERIAL PRIMARY KEY,
        dealership_id     INTEGER NOT NULL REFERENCES dealerships(id),
        vin               VARCHAR(17) NOT NULL,
        stock_number      VARCHAR(50),
        condition         VARCHAR(10),
        year              INTEGER,
        make              VARCHAR(100),
        model             VARCHAR(100),
        trim              VARCHAR(100),
        body_style        VARCHAR(100),
        drivetrain        VARCHAR(100),
        fuel_type         VARCHAR(100),
        engine            VARCHAR(100),
        exterior_color    VARCHAR(50),
        interior_color    VARCHAR(50),
        mileage           INTEGER,
        price             INTEGER,
        listing_url       VARCHAR(2048),
        photo_urls        TEXT,
        carfax_url        VARCHAR(2048),
        vehicle_data      TEXT,
        source            VARCHAR(30) NOT NULL DEFAULT 'extension_sync',
        status            VARCHAR(20) NOT NULL DEFAULT 'active',
        synced_by_user_id INTEGER REFERENCES users(id),
        first_seen_at     TIMESTAMP NOT NULL DEFAULT now(),
        last_seen_at      TIMESTAMP NOT NULL DEFAULT now(),
        removed_at        TIMESTAMP,
        created_at        TIMESTAMP NOT NULL DEFAULT now(),
        updated_at        TIMESTAMP NOT NULL DEFAULT now(),
        CONSTRAINT uq_dealership_inventory_dealership_vin UNIQUE (dealership_id, vin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_dealership_inventory_dealership_id "
    "ON dealership_inventory (dealership_id)",
    "CREATE INDEX IF NOT EXISTS ix_dealership_inventory_dealership_stock "
    "ON dealership_inventory (dealership_id, stock_number)",
    "CREATE INDEX IF NOT EXISTS ix_dealership_inventory_dealership_status "
    "ON dealership_inventory (dealership_id, status)",
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
