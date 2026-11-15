"""
Production migration (PostgreSQL / AWS RDS) for Step 2 — the Dealership tab.

Additive + idempotent. Safe to run twice (second run is a clean no-op).

  1a. dealership_inventory: + specs_json, specs_source, specs_captured_at, body_class
  1b. new table inventory_highlights (+ index + unique constraint)
  1c. users.highlights_language  varchar(2) NOT NULL DEFAULT 'en'
  backfill: dealership_inventory.body_class from body_style (only where NULL),
            using the SAME mapping as app/services/body_style.normalize_body_style
            (ported from the extension's normalizeBodyStyle) — single source of truth.

Dev runs create_all for NEW tables (inventory_highlights) but NEVER ALTERs an
existing table, so the ADD COLUMNs + the backfill must run here on dev too.

Usage:
    cd ~/orbitads/backend
    # preview only — prints the DDL + the backfill counts, changes nothing:
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_step2_dealership_tab --dry-run
    # apply:
    /home/ubuntu/orbitads/venv/bin/python -m scripts.migrate_step2_dealership_tab
"""
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from app.services.body_style import normalize_body_style

load_dotenv()

DDL_STATEMENTS = [
    # 1a — dealership_inventory new columns
    "ALTER TABLE dealership_inventory ADD COLUMN IF NOT EXISTS specs_json TEXT",
    "ALTER TABLE dealership_inventory ADD COLUMN IF NOT EXISTS specs_source VARCHAR(20)",
    "ALTER TABLE dealership_inventory ADD COLUMN IF NOT EXISTS specs_captured_at TIMESTAMP",
    "ALTER TABLE dealership_inventory ADD COLUMN IF NOT EXISTS body_class VARCHAR(20)",
    # 1c — users.highlights_language
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS highlights_language VARCHAR(2) NOT NULL DEFAULT 'en'",
    # 1b — inventory_highlights table (matches SQLModel create_all for the model)
    """
    CREATE TABLE IF NOT EXISTS inventory_highlights (
        id              SERIAL PRIMARY KEY,
        inventory_id    INTEGER NOT NULL REFERENCES dealership_inventory(id),
        language        VARCHAR(2) NOT NULL,
        highlights_json TEXT,
        input_hash      VARCHAR(64) NOT NULL,
        model           VARCHAR(50),
        created_at      TIMESTAMP NOT NULL DEFAULT now(),
        updated_at      TIMESTAMP NOT NULL DEFAULT now(),
        CONSTRAINT uq_inventory_highlights_inventory_language UNIQUE (inventory_id, language)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_inventory_highlights_inventory_id "
    "ON inventory_highlights (inventory_id)",
]


async def _plan_backfill(conn):
    """
    Return [(body_style, body_class, count)] for rows that would be backfilled
    (body_class IS NULL AND body_style IS NOT NULL). Computed with the Python
    normalizer so dry-run and apply agree exactly.
    """
    rows = await conn.fetch(
        "SELECT body_style, COUNT(*) AS n FROM dealership_inventory "
        "WHERE body_class IS NULL AND body_style IS NOT NULL "
        "GROUP BY body_style ORDER BY n DESC"
    )
    plan = []
    for r in rows:
        bs = r["body_style"]
        plan.append((bs, normalize_body_style(bs), r["n"]))
    return plan


async def migrate(dry_run: bool):
    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        if dry_run:
            print("=== DRY RUN — no changes will be made ===\n")
            print("DDL that WOULD run:")
            for stmt in DDL_STATEMENTS:
                print("  " + " ".join(stmt.split()))
            print()
            # body_class may not exist yet on a fresh DB — guard the plan query.
            has_bc = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name='dealership_inventory' AND column_name='body_class')"
            )
            if not has_bc:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM dealership_inventory WHERE body_style IS NOT NULL"
                )
                print(f"body_class column not present yet; after add, backfill candidates "
                      f"(body_style NOT NULL): {total}")
                # Show the mapping it would apply.
                rows = await conn.fetch(
                    "SELECT body_style, COUNT(*) AS n FROM dealership_inventory "
                    "WHERE body_style IS NOT NULL GROUP BY body_style ORDER BY n DESC"
                )
                for r in rows:
                    print(f"    {r['n']:>4}  {r['body_style']!r} -> {normalize_body_style(r['body_style'])!r}")
            else:
                plan = await _plan_backfill(conn)
                tot = sum(n for _, _, n in plan)
                print(f"body_class backfill (rows with body_class NULL & body_style NOT NULL): {tot}")
                for bs, bc, n in plan:
                    print(f"    {n:>4}  {bs!r} -> {bc!r}")
            print("\n=== DRY RUN complete — nothing written ===")
            return

        # ── Apply ──────────────────────────────────────────────
        for stmt in DDL_STATEMENTS:
            await conn.execute(stmt)
            print("OK:", " ".join(stmt.split())[:72])

        # Backfill body_class only where NULL (idempotent — 2nd run finds none).
        plan = await _plan_backfill(conn)
        backfilled = 0
        for bs, bc, n in plan:
            if bc is None:
                continue
            res = await conn.execute(
                "UPDATE dealership_inventory SET body_class=$1 "
                "WHERE body_style=$2 AND body_class IS NULL",
                bc, bs,
            )
            # res like 'UPDATE <n>'
            try:
                backfilled += int(res.split()[-1])
            except Exception:
                pass
            print(f"  backfill {n:>4}  {bs!r} -> {bc!r}")
        print(f"body_class backfilled rows: {backfilled}")
        print("Migration complete.")
    finally:
        await conn.close()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(migrate(dry))
