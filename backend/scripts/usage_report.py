"""
Usage / cost-attribution report. Works on BOTH dev (SQLite) and prod (Postgres)
by reading DATABASE_URL and picking the driver automatically.

Run:
    cd ~/orbitads/backend && source venv/bin/activate   # prod (EC2)
    python -m scripts.usage_report                       # all-time + per-month
    python -m scripts.usage_report 2026-07               # focus one month

Locally (dev):
    cd orbitads/backend && python3 -m scripts.usage_report

WHAT IT COUNTS (recorded in the DB):
  - Videos generated  -> `jobs` (each = 1 Claude SCRIPT call + 1 ElevenLabs
    voiceover + 1 Shotstack render). NOTE: photos-only ads never hit the
    backend, so they are NOT in `jobs`.
  - Claude script calls -> jobs where custom_script is empty (a custom script
    skips Claude).
  - AdEvent breakdown, dealer-config Claude tokens, listings, outros, users.

WHAT IT CANNOT COUNT (not persisted anywhere):
  - Photo classifications (/photos/classify) and caption generations
    (/listings/generate*). Get those Claude call totals from the Anthropic
    console (console.anthropic.com -> Usage).
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

MONTH = sys.argv[1] if len(sys.argv) > 1 else None  # e.g. "2026-07"

# Each query returns a single scalar unless it's a GROUP BY (returns rows).
def scalar_queries(where=""):
    return {
        "jobs_total":        f"SELECT COUNT(*) FROM jobs WHERE 1=1 {where}",
        "jobs_completed":    f"SELECT COUNT(*) FROM jobs WHERE status='completed' {where}",
        "jobs_failed":       f"SELECT COUNT(*) FROM jobs WHERE status='failed' {where}",
        "claude_scripts":    f"SELECT COUNT(*) FROM jobs WHERE (custom_script IS NULL OR custom_script='') {where}",
        "listings":          f"SELECT COUNT(*) FROM listings WHERE 1=1 {where}",
        "outros":            f"SELECT COUNT(*) FROM outro_videos WHERE 1=1 {where}",
        "users":             f"SELECT COUNT(*) FROM users WHERE 1=1 {where}",
    }


def month_where(month):
    # month = 'YYYY-MM'. String-literal comparison works on SQLite (TEXT) and PG (timestamp).
    y, m = month.split("-")
    start = f"{y}-{m}-01"
    nm, ny = (int(m) % 12 + 1), (int(y) + (1 if m == "12" else 0))
    end = f"{ny}-{nm:02d}-01"
    return f"AND created_at >= '{start}' AND created_at < '{end}'"


async def run():
    url = os.getenv("DATABASE_URL", "")
    is_sqlite = "sqlite" in url
    label = "DEV (SQLite)" if is_sqlite else "PROD (PostgreSQL)"

    if is_sqlite:
        import sqlite3
        path = url.split("///")[-1]
        conn = sqlite3.connect(path)
        def one(sql): return conn.execute(sql).fetchone()[0]
        def rows(sql): return conn.execute(sql).fetchall()
        close = conn.close
    else:
        import asyncpg
        conn = await asyncpg.connect(url.replace("+asyncpg", ""))
        async def one(sql): return await conn.fetchval(sql)
        async def rows(sql): return [tuple(r) for r in await conn.fetch(sql)]
        close = conn.close

    async def A(sql):  # await-or-not helper
        r = one(sql) if is_sqlite else await one(sql)
        return r
    async def R(sql):
        r = rows(sql) if is_sqlite else await rows(sql)
        return r

    def hdr(t): print(f"\n===== {t} =====")

    hdr(label)

    async def block(title, where):
        print(f"\n--- {title} ---")
        for k, sql in scalar_queries(where).items():
            print(f"  {k:16} {await A(sql)}")
        print("  AdEvent by type:")
        for et, n in await R(f"SELECT event_type, COUNT(*) FROM ad_events WHERE 1=1 {where} GROUP BY event_type ORDER BY 2 DESC"):
            print(f"    {et:22} {n}")
        dp = await R(f"SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM dealer_platforms WHERE 1=1 {where}")
        c0, it, ot = dp[0]
        print(f"  dealer_configs: {c0}  (claude tokens in={it}, out={ot})")
        # api_usage log (photo_classification, etc.) — units = actual API calls
        # (a classify request logs quantity = # photos = # Claude vision calls).
        # Guarded so the report still runs before the table/migration exists.
        try:
            au = await R(f"SELECT call_type, COUNT(*), COALESCE(SUM(quantity),0), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM api_usage WHERE 1=1 {where} GROUP BY call_type ORDER BY 3 DESC")
            print("  api_usage (logged API calls; units = # calls):")
            for ct, rows_, qty, ait, aot in (au or [("(none)", 0, 0, 0, 0)]):
                print(f"    {ct:22} requests={rows_} units={qty} tok_in={ait} tok_out={aot}")
        except Exception as e:  # noqa: BLE001
            print(f"  api_usage: table not present yet ({str(e).splitlines()[0]})")

    await block("ALL-TIME", "")
    if MONTH:
        await block(f"MONTH {MONTH}", month_where(MONTH))
    else:
        print("\n--- jobs per month ---")
        col = ("substr(created_at,1,7)" if is_sqlite else "to_char(created_at,'YYYY-MM')")
        for ym, n in await R(f"SELECT {col} m, COUNT(*) FROM jobs GROUP BY m ORDER BY m"):
            print(f"    {ym}  {n}")

    print("\nNOTE: photo classifications and caption generations are NOT in the DB.")
    print("Total Claude call count = Anthropic console (console.anthropic.com -> Usage).")

    if is_sqlite:
        close()
    else:
        await close()


if __name__ == "__main__":
    asyncio.run(run())
