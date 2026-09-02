"""
Schema parity diagnostic: dump data_type + udt_name for every timestamp*
column across every user table, so dev and prod can be compared side by side.

Run on EC2:
    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.diag_schema_parity 2>&1
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.core.database import AsyncSessionLocal
from sqlmodel import text

QUERY = """
SELECT
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND data_type LIKE 'timestamp%'
ORDER BY table_name, column_name
"""

async def main():
    async with AsyncSessionLocal() as session:
        rows = (await session.exec(text(QUERY))).all()
    if not rows:
        print("(no timestamp columns found)")
        return
    print(f"{'table':<35} {'column':<35} {'data_type':<40} {'udt_name'}")
    print("-" * 130)
    for table_name, column_name, data_type, udt_name in rows:
        print(f"{table_name:<35} {column_name:<35} {data_type:<40} {udt_name}")

asyncio.run(main())
