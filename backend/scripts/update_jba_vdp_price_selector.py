"""
One-off prod fix (2026-11-15): set the JBA Kia config's DETAIL-PAGE (VDP) price
selector so it reads the final "JBA Price" on BOTH used and new cars.

Why: the current VDP selector is `dd.final-price.internetPrice .price-value`,
which only matches USED cars — new cars render `dd.final-price` WITHOUT the
`.internetPrice` modifier, so new-car VDP price came back NULL (confirmed in
reports/2026-11-12-step2-s1-diagnosis.md §3). The combined selector below keeps
the exact proven used-car clause and adds a second clause that matches new cars
too (and used, redundantly) — so used-car behavior is unchanged.

This mirrors scripts/update_jba_card_price_selector.py (which fixed the CARD
price selector); this one fixes the DETAIL-PAGE (detail_page.sale_price) selector.

Dry run by default. Prints the matching config and the before/after value.

    cd ~/orbitads/backend
    /home/ubuntu/orbitads/venv/bin/python -m scripts.update_jba_vdp_price_selector           # dry run
    /home/ubuntu/orbitads/venv/bin/python -m scripts.update_jba_vdp_price_selector --apply    # write

Rollback: re-run with --apply --value "<the 'before' value printed by the dry run>".
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Explicit path: find_dotenv() fails when python reads the script from stdin.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

NEW_VALUE = (
    "dl.pricing-detail dd.final-price.internetPrice .price-value, "
    "dl.pricing-detail dd.final-price span.price-value"
)
JBA_DOMAINS = ["jbakia.com", "www.jbakia.com"]


async def main(apply: bool, value: str) -> None:
    conn = await asyncpg.connect(os.getenv("DATABASE_URL").replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            """
            SELECT id, config_json::text AS cfg, source_url
            FROM dealer_platforms
            WHERE status = 'active' AND (
                id IN (SELECT platform_id FROM dealer_platform_domains WHERE domain = ANY($1::text[]))
                OR source_url ILIKE '%jbakia.com%'
            )
            """,
            JBA_DOMAINS,
        )
        if len(rows) != 1:
            print("STOP: expected exactly 1 active JBA config, found:",
                  [(r["id"], r["source_url"]) for r in rows])
            return

        pid = rows[0]["id"]
        cfg = json.loads(rows[0]["cfg"])
        detail = cfg.get("detail_page")
        if not isinstance(detail, dict):
            print(f"STOP: config {pid} has no detail_page")
            return

        print(f"config id: {pid}  source_url: {rows[0]['source_url']}")
        print(f"before: {detail.get('sale_price')}")
        print(f"new:    {value}")

        if not apply:
            print("DRY RUN — nothing written. Re-run with --apply to write.")
            return

        detail["sale_price"] = value
        await conn.execute(
            "UPDATE dealer_platforms SET config_json = $1::json WHERE id = $2",
            json.dumps(cfg), pid,
        )
        after = await conn.fetchval(
            "SELECT config_json->'detail_page'->>'sale_price' FROM dealer_platforms WHERE id = $1",
            pid,
        )
        print(f"after:  {after}")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--value", default=NEW_VALUE)
    args = ap.parse_args()
    asyncio.run(main(args.apply, args.value))
