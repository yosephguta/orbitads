"""
Read-only export of dealer platform config(s) for a given domain.

Safe on prod: SELECT only, no writes.

Outputs:
  1. JSON (stdout, or --out <file>) in exactly the format
     scripts/import_prod_configs.py consumes:
       { "configs": [...], "blocked_photo_hosts": [] }
     No HTML fragments (source_html_fragments is excluded).
  2. Human-readable summary on stdout: matching dealerships + their users
     (email, role, subscription_status, purchased_plan — no tokens or hashes).

Usage:
    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.export_dealer_config
    /home/ubuntu/orbitads/venv/bin/python -m scripts.export_dealer_config --domain jbakia.com
    /home/ubuntu/orbitads/venv/bin/python -m scripts.export_dealer_config --domain jbakia.com --out /tmp/jba_config.json
"""
import argparse
import asyncio
import json
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


def _bare(domain: str) -> str:
    d = domain.lower().strip()
    return d[4:] if d.startswith("www.") else d


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export dealer config for a domain (read-only, safe on prod)."
    )
    parser.add_argument(
        "--domain", default="jbakia.com",
        help="Domain to look up (default: jbakia.com). www. prefix is ignored.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Write the JSON part to this file path (stdout only if omitted).",
    )
    args = parser.parse_args()

    bare = _bare(args.domain)
    variants = [bare, f"www.{bare}"]

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
    try:
        platform_ids: set = set()

        # Match via dealer_platform_domains
        rows = await conn.fetch(
            "SELECT platform_id FROM dealer_platform_domains "
            "WHERE domain = ANY($1::text[])",
            variants,
        )
        for r in rows:
            platform_ids.add(r["platform_id"])

        # Match via source_url on the platform itself
        rows = await conn.fetch(
            "SELECT id FROM dealer_platforms "
            "WHERE source_url ILIKE $1 OR source_url ILIKE $2",
            f"%{bare}%", f"%www.{bare}%",
        )
        for r in rows:
            platform_ids.add(r["id"])

        # Match via dealerships.website_url → platform_id
        rows = await conn.fetch(
            "SELECT platform_id FROM dealerships "
            "WHERE (website_url ILIKE $1 OR website_url ILIKE $2) "
            "AND platform_id IS NOT NULL",
            f"%{bare}%", f"%www.{bare}%",
        )
        for r in rows:
            platform_ids.add(r["platform_id"])

        if not platform_ids:
            print(f"No dealer_platforms found for domain: {args.domain}")
            return

        # Build config entries
        configs = []
        for pid in sorted(platform_ids):
            p = await conn.fetchrow(
                "SELECT id, name, platform_slug, config_json, status, source_url, "
                "notes, generation_warnings FROM dealer_platforms WHERE id = $1",
                pid,
            )
            if p is None:
                continue

            domain_rows = await conn.fetch(
                "SELECT domain FROM dealer_platform_domains "
                "WHERE platform_id = $1 ORDER BY domain",
                pid,
            )
            domains = [r["domain"] for r in domain_rows]

            config_json = p["config_json"]
            if isinstance(config_json, str):
                config_json = json.loads(config_json)

            gen_warn = p["generation_warnings"]
            if isinstance(gen_warn, str):
                gen_warn = json.loads(gen_warn)

            configs.append({
                "platform_slug": p["platform_slug"],
                "name": p["name"],
                "source_url": p["source_url"],
                "status": p["status"],
                "config_json": config_json,
                "notes": p["notes"],
                "generation_warnings": gen_warn or [],
                "domains": domains,
            })

        seed = {"configs": configs, "blocked_photo_hosts": []}

        # --- JSON output ---
        json_str = json.dumps(seed, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(json_str)
            print(f"JSON written to: {args.out}")
        else:
            print("=== JSON (import_prod_configs.py format) ===")
            print(json_str)

        # --- Human-readable summary ---
        print(f"\n=== Platforms matched for '{args.domain}' ===")
        for c in configs:
            pid_val = next(
                (pid for pid in sorted(platform_ids)
                 if True), "?"
            )
            print(f"  platform_slug : {c['platform_slug']}")
            print(f"  name          : {c['name']}")
            print(f"  source_url    : {c['source_url']}")
            print(f"  status        : {c['status']}")
            print(f"  domains       : {c['domains']}")
            print()

        # Dealerships using these platforms
        ds_rows = await conn.fetch(
            """
            SELECT id, dealer_group, dealership_name, platform_id,
                   required_tagline, required_tagline_es
            FROM dealerships
            WHERE platform_id = ANY($1::int[])
            ORDER BY id
            """,
            list(platform_ids),
        )

        print("=== Dealerships using these platforms ===")
        if not ds_rows:
            print("  (none)")
        for d in ds_rows:
            print(
                f"  id={d['id']}  group={d['dealer_group']!r}  "
                f"name={d['dealership_name']!r}  platform_id={d['platform_id']}"
            )
            if d["required_tagline"]:
                print(f"    required_tagline:    {d['required_tagline']!r}")
            if d["required_tagline_es"]:
                print(f"    required_tagline_es: {d['required_tagline_es']!r}")

            users = await conn.fetch(
                """
                SELECT email, role, subscription_status, purchased_plan
                FROM users WHERE dealership_id = $1 ORDER BY email
                """,
                d["id"],
            )
            if users:
                for u in users:
                    print(
                        f"    user: {u['email']}"
                        f"  role={u['role']}"
                        f"  status={u['subscription_status']}"
                        f"  plan={u['purchased_plan']}"
                    )
            else:
                print("    (no users in this dealership)")
            print()

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
