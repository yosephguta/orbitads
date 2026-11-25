"""
Pre-restart schema guard for DealersOrbit backend.

Imports every SQLModel table model (all of app/models/) and verifies that
each table and each model column exists in the connected PostgreSQL database.

  missing table  → FAIL (exit 1)
  missing column → FAIL (exit 1)
  type mismatch  → WARN only  (the known timestamptz drift must never fail)
  extra DB cols  → ignored    (old columns are always allowed)

Usage (run from ~/orbitads/backend):
    /home/ubuntu/orbitads/venv/bin/python -m scripts.check_schema
    /home/ubuntu/orbitads/venv/bin/python -m scripts.check_schema --schema public

Exit 0  → SCHEMA OK
Exit 1  → SCHEMA CHECK FAILED — do not restart
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Dict, Set

# --- Explicit .env load so this works regardless of CWD ---
# find_dotenv() / relative "env_file" both fail when the script is invoked
# from a different directory or fed to python via stdin.
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path, override=False)

# --- Make app.* importable when invoked from outside backend/ ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402  (import after sys.path fix)
from sqlmodel import SQLModel  # noqa: E402

# Import every table model so SQLModel.metadata.tables is fully populated.
# Do NOT import app.main — that starts the full FastAPI app.
from app.models.ad_event import AdEvent  # noqa
from app.models.api_usage import ApiUsage  # noqa
from app.models.blocked_photo_host import BlockedPhotoHost  # noqa
from app.models.dealer_platform import DealerPlatform  # noqa
from app.models.dealer_platform_domain import DealerPlatformDomain  # noqa
from app.models.dealership import Dealership  # noqa
from app.models.dealership_inventory import DealershipInventory  # noqa
from app.models.inventory_highlights import InventoryHighlights  # noqa
from app.models.job import Job  # noqa
from app.models.listing import Listing  # noqa
from app.models.outro_video import OutroVideo  # noqa
from app.models.partial_lead import PartialLead  # noqa
from app.models.photo_classification_cache import PhotoClassificationCache  # noqa
from app.models.saved_script import SavedScript  # noqa
from app.models.user import User  # noqa

# SQLAlchemy type name → acceptable PostgreSQL information_schema data_type values.
# A DB type NOT in the acceptable set emits WARN (never FAIL — type mismatches
# are always WARN-only). DateTime's canonical type is 'timestamp without time zone'
# (what create_all produces); 'timestamp with time zone' is intentionally excluded
# so the known 13-column timestamptz drift in prod is surfaced as WARN.
# See docs/backend-data-model.md for the full drift inventory.
_SA_TO_PG: Dict[str, Set[str]] = {
    "AutoString":   {"character varying", "text"},   # max_length → varchar, bare → text
    "String":       {"character varying", "text"},
    "Text":         {"text", "character varying"},
    "DateTime":     {"timestamp without time zone"},  # timestamptz drift → WARN
    "Integer":      {"integer", "bigint"},
    "BigInteger":   {"bigint", "integer"},
    "Boolean":      {"boolean"},
    "Float":        {"real", "double precision", "numeric"},
    "Numeric":      {"numeric", "real", "double precision"},
    "JSON":         {"json", "jsonb"},
}


async def check_schema(schema: str = "public") -> int:
    """Return exit code: 0 = OK, 1 = FAILED."""
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL not set — check that .env was found")
        return 1

    conn_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    try:
        conn = await asyncpg.connect(conn_url, server_settings={"timezone": "UTC"})
    except Exception as exc:
        print(f"ERROR: could not connect to database: {exc}")
        return 1

    try:
        rows = await conn.fetch(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = $1",
            schema,
        )
    finally:
        await conn.close()

    # Build lookup: table_name → {column_name → data_type}
    db_schema: Dict[str, Dict[str, str]] = {}
    for row in rows:
        tbl = row["table_name"]
        db_schema.setdefault(tbl, {})[row["column_name"]] = row["data_type"]

    problems = []
    warnings = []

    for sa_table in SQLModel.metadata.tables.values():
        tname = sa_table.name
        if tname not in db_schema:
            problems.append(f"FAIL: table '{tname}' missing from DB")
            continue

        db_cols = db_schema[tname]
        for col in sa_table.columns:
            cname = col.name
            if cname not in db_cols:
                problems.append(f"FAIL: column '{tname}.{cname}' missing from DB")
                continue

            sa_type_name = type(col.type).__name__
            acceptable = _SA_TO_PG.get(sa_type_name)
            if acceptable is not None:
                db_type = db_cols[cname]
                if db_type not in acceptable:
                    warnings.append(
                        f"WARN: '{tname}.{cname}' — model type={sa_type_name}, "
                        f"db type={db_type!r} (expected one of: {sorted(acceptable)})"
                    )

    for w in warnings:
        print(w)
    for p in problems:
        print(p)

    if problems:
        print("SCHEMA CHECK FAILED — do not restart")
        return 1

    print("SCHEMA OK")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Pre-restart schema guard: verify every model column exists in the DB."
    )
    ap.add_argument(
        "--schema",
        default="public",
        help="PostgreSQL schema to check (default: public)",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(check_schema(args.schema)))


if __name__ == "__main__":
    main()
