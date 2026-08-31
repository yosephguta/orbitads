"""
Seed prod (PostgreSQL / AWS RDS) with the known-good dealer configs from
scripts/prod_config_seed.json (Antwerpen, Apple Ford, AG Auto, Tate, Dawson).

Idempotent: skips any config whose domain is already mapped, so it's safe to
re-run. Inserts each DealerPlatform as 'active', maps its domain(s) via
dealer_platform_domains, and seeds blocked_photo_hosts.

PREREQUISITE — run the table migrations first:
    python -m scripts.migrate_add_dealer_platform_domains
    python -m scripts.migrate_add_source_html_fragments
    python -m scripts.migrate_add_blocked_photo_hosts

Then:
    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.import_prod_configs
"""
import asyncio
import json
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()

SEED_PATH = os.path.join(os.path.dirname(__file__), "prod_config_seed.json")


async def main():
    with open(SEED_PATH) as f:
        seed = json.load(f)

    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        inserted, skipped = 0, 0
        for c in seed["configs"]:
            domains = [d.lower().strip() for d in c.get("domains", []) if d]
            if not domains:
                print(f"  SKIP {c['platform_slug']}: no domains")
                continue

            # Idempotency: if any domain is already mapped, assume seeded.
            existing = await conn.fetchval(
                "SELECT platform_id FROM dealer_platform_domains WHERE domain = ANY($1::text[])",
                domains,
            )
            if existing is not None:
                print(f"  SKIP {c['platform_slug']} ({domains[0]}): already mapped -> platform {existing}")
                skipped += 1
                continue

            platform_id = await conn.fetchval(
                """
                INSERT INTO dealer_platforms
                  (name, platform_slug, config_json, status, source_url, notes,
                   generation_warnings, reviewed_at, reviewed_by, created_at)
                VALUES ($1,$2,$3::json,'active',$4,$5,$6::json,NOW(),'seed',NOW())
                RETURNING id
                """,
                c["name"], c["platform_slug"], json.dumps(c["config_json"]),
                c["source_url"], c.get("notes"),
                json.dumps(c.get("generation_warnings") or []),
            )
            for d in domains:
                await conn.execute(
                    "INSERT INTO dealer_platform_domains (domain, platform_id, created_at) "
                    "VALUES ($1,$2,NOW()) ON CONFLICT (domain) DO NOTHING",
                    d, platform_id,
                )
            print(f"  ADDED {c['platform_slug']} -> platform {platform_id}, domains {domains}")
            inserted += 1

        for b in seed.get("blocked_photo_hosts", []):
            await conn.execute(
                "INSERT INTO blocked_photo_hosts (hostname, source, created_at) "
                "VALUES ($1,$2,NOW()) ON CONFLICT (hostname) DO NOTHING",
                b["hostname"], b.get("source", "seed"),
            )
        print(f"\nConfigs: {inserted} added, {skipped} skipped. "
              f"Blocked hosts seeded: {len(seed.get('blocked_photo_hosts', []))} (idempotent).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
