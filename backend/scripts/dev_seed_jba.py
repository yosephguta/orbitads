"""
Seed the DEV database with the JBA Kia config, dealership, and two test accounts.

Idempotent: safe to run multiple times. Skips any step that is already done.
Refuses to run against any non-localhost DATABASE_URL (never touches prod).

Prerequisites:
  - jba_config_from_prod.json must exist in this directory (output of export_dealer_config.py)
  - Local Postgres running and dealersorbit_dev schema initialised (uvicorn startup does create_all)

Usage:
    cd ~/Desktop/dealersorbit/orbitads/backend
    python3 -m scripts.dev_seed_jba
"""
import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()

SEED_PATH = os.path.join(os.path.dirname(__file__), "jba_config_from_prod.json")
PASSWORD_HASH = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("testpass123")

MANAGER_EMAIL = "jba-manager@dealersorbit.com"
REP_EMAIL = "jba-rep@dealersorbit.com"
DEALERSHIP_GROUP = "JBA Auto Group"
DEALERSHIP_NAME = "JBA Kia Glen Burnie"
DEALERSHIP_LOCATION = "Glen Burnie, MD"


def _bare(url: str) -> str:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


async def main() -> None:
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        sys.exit("DATABASE_URL not set")

    # Safety: refuse to run against anything that is not localhost
    is_local = any(
        marker in db_url
        for marker in ("localhost", "127.0.0.1", "::1", "@localhost", "@127.")
    )
    if not is_local:
        sys.exit(
            "ERROR: DATABASE_URL does not point at localhost — refusing to run.\n"
            f"  URL: {db_url}\n"
            "This script is dev-only."
        )

    if not os.path.exists(SEED_PATH):
        sys.exit(f"ERROR: {SEED_PATH} not found. Run export_dealer_config.py on prod first.")

    with open(SEED_PATH) as f:
        seed = json.load(f)

    conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
    try:
        # ── 1. Import JBA platform config (reuse import_prod_configs.py logic) ──
        platform_id = None
        for c in seed["configs"]:
            domains = [d.lower().strip() for d in c.get("domains", []) if d]
            if not domains or c.get("status") != "active":
                print(f"  SKIP config {c['platform_slug']!r}: no domains or not active")
                continue

            # Keep only jbakia.com / www.jbakia.com
            jba_domains = [d for d in domains if "jbakia.com" in d]
            if not jba_domains:
                print(f"  SKIP config {c['platform_slug']!r}: no jbakia.com domain")
                continue

            existing = await conn.fetchval(
                "SELECT platform_id FROM dealer_platform_domains "
                "WHERE domain = ANY($1::text[])",
                jba_domains,
            )
            if existing is not None:
                platform_id = existing
                print(f"  SKIP platform import: jbakia.com already mapped -> platform {platform_id}")
            else:
                config_json = c["config_json"]
                if isinstance(config_json, str):
                    config_json = json.loads(config_json)

                gen_warn = c.get("generation_warnings") or []

                platform_id = await conn.fetchval(
                    """
                    INSERT INTO dealer_platforms
                      (name, platform_slug, config_json, status, source_url, notes,
                       generation_warnings, reviewed_at, reviewed_by, created_at)
                    VALUES ($1,$2,$3::json,'active',$4,$5,$6::json,NOW(),'dev_seed_jba',NOW())
                    RETURNING id
                    """,
                    c["name"], c["platform_slug"], json.dumps(config_json),
                    c["source_url"], c.get("notes"),
                    json.dumps(gen_warn),
                )
                for d in jba_domains:
                    await conn.execute(
                        "INSERT INTO dealer_platform_domains (domain, platform_id, created_at) "
                        "VALUES ($1,$2,NOW()) ON CONFLICT (domain) DO NOTHING",
                        d, platform_id,
                    )
                # Also ensure www. variant is mapped
                for variant in [d for d in [_bare(c["source_url"]), f"www.{_bare(c['source_url'])}"] if d not in jba_domains]:
                    if "jbakia.com" in variant:
                        await conn.execute(
                            "INSERT INTO dealer_platform_domains (domain, platform_id, created_at) "
                            "VALUES ($1,$2,NOW()) ON CONFLICT (domain) DO NOTHING",
                            variant, platform_id,
                        )
                print(f"  ADDED platform {c['platform_slug']!r} -> id={platform_id}, domains={jba_domains}")

        if platform_id is None:
            sys.exit("ERROR: No active JBA platform config found in jba_config_from_prod.json")

        # ── 2. Create dealership ────────────────────────────────────────────────
        ds_row = await conn.fetchrow(
            "SELECT id, platform_id FROM dealerships WHERE dealership_name = $1 AND dealer_group = $2",
            DEALERSHIP_NAME, DEALERSHIP_GROUP,
        )
        if ds_row:
            dealership_id = ds_row["id"]
            print(f"  SKIP dealership: already exists id={dealership_id}")
            if ds_row["platform_id"] != platform_id:
                await conn.execute(
                    "UPDATE dealerships SET platform_id = $1 WHERE id = $2",
                    platform_id, dealership_id,
                )
                print(f"    Updated platform_id -> {platform_id}")
        else:
            dealership_id = await conn.fetchval(
                """
                INSERT INTO dealerships
                  (dealer_group, dealership_name, location, platform_id, created_at)
                VALUES ($1,$2,$3,$4,NOW())
                RETURNING id
                """,
                DEALERSHIP_GROUP, DEALERSHIP_NAME, DEALERSHIP_LOCATION, platform_id,
            )
            print(f"  ADDED dealership {DEALERSHIP_NAME!r} -> id={dealership_id}")

        # ── 3. Create manager account ───────────────────────────────────────────
        manager_row = await conn.fetchrow(
            "SELECT id, dealership_id, role FROM users WHERE email = $1", MANAGER_EMAIL
        )
        if manager_row:
            manager_id = manager_row["id"]
            print(f"  SKIP manager: {MANAGER_EMAIL} already exists id={manager_id}")
            if manager_row["dealership_id"] != dealership_id or manager_row["role"] != "manager":
                await conn.execute(
                    "UPDATE users SET dealership_id=$1, role='manager', "
                    "subscription_status='active', purchased_plan='dealership' WHERE id=$2",
                    dealership_id, manager_id,
                )
                print("    Updated dealership_id/role/plan")
        else:
            manager_id = await conn.fetchval(
                """
                INSERT INTO users (
                  email, full_name, first_name, last_name, dealership_name, hashed_password,
                  is_active, is_verified,
                  role, subscription_status, purchased_plan, signup_plan,
                  dealership_id,
                  elevenlabs_voice_id, elevenlabs_voice_id_es,
                  outro_volume, preferred_language, trial_video_count, dealer_config_requested,
                  created_at, updated_at
                ) VALUES (
                  $1,'JBA Manager','JBA','Manager','JBA Kia Glen Burnie',$2,
                  true,true,
                  'manager','active','dealership','dealership',
                  $3,
                  'Gubgw9l4dtIoQA9YZHgx','zDMHo7CPscBTgfDtPOWl',
                  2.0,'en',0,false,
                  NOW(),NOW()
                ) RETURNING id
                """,
                MANAGER_EMAIL, PASSWORD_HASH, dealership_id,
            )
            print(f"  ADDED manager {MANAGER_EMAIL} -> id={manager_id}")

        # Set as dealership's manager_user_id
        await conn.execute(
            "UPDATE dealerships SET manager_user_id=$1 WHERE id=$2",
            manager_id, dealership_id,
        )

        # ── 4. Create rep account ───────────────────────────────────────────────
        rep_row = await conn.fetchrow(
            "SELECT id, dealership_id, role FROM users WHERE email = $1", REP_EMAIL
        )
        if rep_row:
            rep_id = rep_row["id"]
            print(f"  SKIP rep: {REP_EMAIL} already exists id={rep_id}")
            if rep_row["dealership_id"] != dealership_id or rep_row["role"] != "salesperson":
                await conn.execute(
                    "UPDATE users SET dealership_id=$1, role='salesperson' WHERE id=$2",
                    dealership_id, rep_id,
                )
                print("    Updated dealership_id/role")
        else:
            rep_id = await conn.fetchval(
                """
                INSERT INTO users (
                  email, full_name, first_name, last_name, dealership_name, hashed_password,
                  is_active, is_verified,
                  role, subscription_status, purchased_plan, signup_plan,
                  dealership_id,
                  elevenlabs_voice_id, elevenlabs_voice_id_es,
                  outro_volume, preferred_language, trial_video_count, dealer_config_requested,
                  created_at, updated_at
                ) VALUES (
                  $1,'JBA Rep','JBA','Rep','JBA Kia Glen Burnie',$2,
                  true,true,
                  'salesperson','trial',NULL,'dealership',
                  $3,
                  'Gubgw9l4dtIoQA9YZHgx','zDMHo7CPscBTgfDtPOWl',
                  2.0,'en',0,false,
                  NOW(),NOW()
                ) RETURNING id
                """,
                REP_EMAIL, PASSWORD_HASH, dealership_id,
            )
            print(f"  ADDED rep {REP_EMAIL} -> id={rep_id}")

        # ── Summary ────────────────────────────────────────────────────────────
        print()
        print("=== Dev seed complete ===")
        print(f"  platform_id  : {platform_id}")
        print(f"  dealership_id: {dealership_id}  ({DEALERSHIP_NAME})")
        print(f"  manager      : {MANAGER_EMAIL}  id={manager_id}")
        print(f"  rep          : {REP_EMAIL}  id={rep_id}")
        print(f"  password     : testpass123")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
