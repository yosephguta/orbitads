"""
Diagnostic: read real listings rows through the application's actual
AsyncSessionLocal/SQLModel/asyncpg session stack and print repr() + tzinfo
for all 5 timestamp columns. Also runs the information_schema parity query.

Run on EC2:
    /home/ubuntu/orbitads/venv/bin/python -m scripts.diag_prod_listing_tzinfo
from ~/orbitads/backend/
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlmodel import select, text
from app.core.database import AsyncSessionLocal
from app.models.listing import Listing
from datetime import datetime, timezone


TIMESTAMP_COLS = (
    "created_at",
    "fb_posted_at",
    "last_checked_at",
    "sold_detected_at",
    "updated_at",
)

# Exact copy of user_activity._strip — do not simplify.
def _strip(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


async def main():
    async with AsyncSessionLocal() as session:

        # ── 1. Parity query — what does the prod DB actually say? ─────────
        print("=" * 70)
        print("information_schema column types — listings table (PROD):")
        type_rows = (await session.exec(
            text(
                "SELECT column_name, data_type, udt_name "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='listings' "
                "AND data_type LIKE 'timestamp%' "
                "ORDER BY column_name"
            )
        )).all()
        for col_name, data_type, udt_name in type_rows:
            print(f"  {col_name:30s}  data_type={data_type!r:50s}  udt_name={udt_name!r}")

        # ── 2. Rows with non-null fb_posted_at ────────────────────────────
        print()
        print("=" * 70)
        print("Listings rows with non-null fb_posted_at (up to 3):")
        rows = (await session.exec(
            select(Listing).where(Listing.fb_posted_at.isnot(None)).limit(3)
        )).all()

        if not rows:
            print("  (none found — no posted listings in prod)")
        else:
            # Simulate the naive since cutoff parse_dt produces for "90d ago".
            naive_since = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            naive_since = naive_since.replace(
                day=max(1, naive_since.day - 90 % 30)
            )
            # Use a fixed past date that all real rows should be after.
            naive_since_fixed = datetime(2026, 1, 1, 0, 0, 0)  # naive UTC

            for lst in rows:
                print(f"\n  listing.id={lst.id}")
                for col in TIMESTAMP_COLS:
                    val = getattr(lst, col)
                    if val is None:
                        print(f"    {col:20s}: None")
                        continue
                    stripped = _strip(val)
                    # Attempt the actual comparison the drill-down performs.
                    try:
                        cmp_result = stripped < naive_since_fixed
                        cmp_ok = f"stripped < naive_since → {cmp_result}  ✅ no TypeError"
                    except TypeError as e:
                        cmp_ok = f"stripped < naive_since → TypeError: {e}  ❌ BUG"

                    print(f"    {col:20s}:")
                    print(f"      raw repr  = {repr(val)}")
                    print(f"      raw tzinfo= {val.tzinfo}")
                    print(f"      _strip()  = {repr(stripped)}")
                    print(f"      {cmp_ok}")

        # ── 3. Also show what _strip does with an explicitly aware value ──
        print()
        print("=" * 70)
        print("_strip() trace — explicit aware UTC value (sanity check):")
        aware = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = _strip(aware)
        print(f"  input  = {repr(aware)}")
        print(f"  output = {repr(result)}")
        print(f"  output tzinfo = {result.tzinfo}")
        naive_since = datetime(2026, 1, 1, 0, 0, 0)
        try:
            print(f"  output < naive_since → {result < naive_since}  ✅")
        except TypeError as e:
            print(f"  output < naive_since → TypeError: {e}  ❌")

        print()
        print("_strip() trace — explicit naive value (sanity check):")
        naive_val = datetime(2026, 8, 15, 12, 0, 0)
        result2 = _strip(naive_val)
        print(f"  input  = {repr(naive_val)}")
        print(f"  output = {repr(result2)}")
        print(f"  output tzinfo = {result2.tzinfo}")
        try:
            print(f"  output < naive_since → {result2 < naive_since}  ✅")
        except TypeError as e:
            print(f"  output < naive_since → TypeError: {e}  ❌")


asyncio.run(main())
