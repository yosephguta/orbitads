"""
Seed the local dev DB with the real listing URLs from the three CSVs the user
exported, so "Check Listings" can be tested end-to-end in a real Chrome against
the local backend.

Run from the backend dir:
    python3 -m scripts.seed_test_listings
    python3 -m scripts.seed_test_listings --email testdealer@dealersorbit.com

What it does:
  - Reads prod_test_listings.csv, prod_job_urls.csv, dev_testdealer_listings.csv
    from ~/Desktop/dealersorbit/ (override with DEALERSORBIT_DIR).
  - Dedupes to one row per vehicle (CarGurus by /details/<id>, Cars.com by
    /vehicledetail/<uuid>, else by VIN, else by clean URL).
  - Inserts Listing rows for the target user, marked fb_posted=True, is_sold=False.
  - Baseline price: the CSVs have NO price column, so baseline is left NULL
    (the first Check Listings run adopts the scraped price silently — no false
    "price changed"). EXCEPT a few known CarGurus VINs get a deliberately-offset
    baseline (SYNTHETIC_BASELINES) so the price-change UI is demoable on run 1.
  - Idempotent: deletes the user's existing rows matching the seeded VINs/URLs
    first, then re-inserts.

DEV ONLY. Does not touch prod.
"""

import asyncio
import csv
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DEALERSORBIT_DIR = os.getenv(
    "DEALERSORBIT_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")),
)
CSVS = ["prod_test_listings.csv", "prod_job_urls.csv", "dev_testdealer_listings.csv"]

# A few AVAILABLE CarGurus listings whose real current price we captured during
# the investigation. Seeding a baseline that DIFFERS from the live price makes
# the "Price changed" notification fire on the very first check (for testing).
# Format matches raw scraped strings. (If the car is since sold/CF-blocked, it
# simply won't produce a price-change — harmless.)
SYNTHETIC_BASELINES = {
    "1FMJU1MT2JEA19412": "$27,797",  # 2018 Ford Expedition (live was ~$25,797)
    "1FATP8EM3F5325228": "$21,297",  # 2015 Ford Mustang    (live was ~$19,297)
    "1C4RJKEG4S8686831": "$50,297",  # 2025 Jeep GC L       (live was ~$48,297)
}

# CarGurus VINs confirmed SOLD during the investigation (404 "already left the
# lot"). Included so the sold path is exercised in a trimmed run.
KNOWN_SOLD_CARGURUS = ["3GCUKREC9HG240318", "1GCGSBE39F1178533"]

# A DIFFERENT batch for --fresh runs (vehicles not in the default mix). Prices +
# sold VINs also captured during the investigation.
FRESH_SYNTHETIC_BASELINES = {
    "KMHDB8AE6AU053800": "$9,797",   # 2010 Hyundai Elantra (live was ~$8,797)
    "1G1RD6E48CU108057": "$10,297",  # 2012 Chevrolet Volt  (live was ~$9,297)
}
FRESH_KNOWN_SOLD_CARGURUS = [
    "3GCUKREC2HG154137",  # 2017 Silverado — "already left the lot"
    "3GCUKTEC2HG443628",  # 2017 Silverado — "already left the lot"
    "3GCUKSEC1HG488909",  # 2017 Silverado — "already left the lot"
]

# Combined baseline lookup (default + fresh).
ALL_BASELINES = {**SYNTHETIC_BASELINES, **FRESH_SYNTHETIC_BASELINES}

# Trim a full run to a balanced ~15-listing mix. Set TRIM=0 (or --all) to seed
# everything. Priority VINs (price-change + known-sold) are always kept.
DEFAULT_TRIM = 15
SOURCE_QUOTA = {"CarGurus": 5, "Cars.com": 4, "jbakia": 4, "other": 2}


def source_of(url):
    h = urlparse(url).hostname or ""
    if "cars.com" in h:
        return "Cars.com"
    if "cargurus.com" in h:
        return "CarGurus"
    if "jbakia" in h:
        return "jbakia"
    return "other"


def trim_mix(records, limit, priority_vins):
    """Keep priority rows (synthetic baselines + known-sold CarGurus), then fill
    up to `limit` with a balanced spread across sources."""
    chosen, chosen_ids = [], set()

    def add(r):
        key = id(r)
        if key in chosen_ids:
            return
        chosen_ids.add(key)
        chosen.append(r)

    for r in records:
        if r.get("vin") in priority_vins:
            add(r)

    # Fill remaining slots respecting per-source quotas.
    counts = {}
    for r in chosen:
        counts[source_of(r["url"])] = counts.get(source_of(r["url"]), 0) + 1
    for r in records:
        if len(chosen) >= limit:
            break
        if id(r) in chosen_ids:
            continue
        src = source_of(r["url"])
        if counts.get(src, 0) >= SOURCE_QUOTA.get(src, 99):
            continue
        add(r)
        counts[src] = counts.get(src, 0) + 1

    return chosen[:limit]


def collapse_key(url, vin):
    h = urlparse(url).hostname or ""
    if "cargurus.com" in h:
        m = re.search(r"/details/(\d+)", url)
        return ("cargurus", m.group(1) if m else url)
    if "cars.com" in h:
        m = re.search(r"/vehicledetail/([^/]+)", url)
        return ("cars", m.group(1) if m else url)
    if vin:
        return ("vin", vin)
    return ("url", url.split("?")[0])


def load_rows():
    rows = {}  # collapse_key -> dict
    for fname in CSVS:
        path = os.path.join(DEALERSORBIT_DIR, fname)
        if not os.path.exists(path):
            print(f"  ! missing {path} — skipping")
            continue
        with open(path) as f:
            for d in csv.DictReader(f):
                url = (d.get("listing_url") or "").strip()
                if not url:
                    continue
                vin = (d.get("vin") or "").strip() or None
                key = collapse_key(url, vin)
                cur = rows.get(key)
                rec = {
                    "url": url, "vin": vin,
                    "year": (d.get("year") or "").strip() or None,
                    "make": (d.get("make") or "").strip() or None,
                    "model": (d.get("model") or "").strip() or None,
                }
                if cur is None:
                    rows[key] = rec
                else:
                    # keep the shortest URL; backfill missing meta
                    if len(url) < len(cur["url"]):
                        cur["url"] = url
                    for fld in ("vin", "year", "make", "model"):
                        if not cur.get(fld) and rec.get(fld):
                            cur[fld] = rec[fld]
    return list(rows.values())


async def main():
    email = "testdealer@dealersorbit.com"
    if "--email" in sys.argv:
        email = sys.argv[sys.argv.index("--email") + 1]

    dsn = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    if not dsn:
        print("DATABASE_URL not set"); return
    if "localhost" not in dsn and "127.0.0.1" not in dsn:
        print(f"REFUSING: DATABASE_URL is not local ({dsn.split('@')[-1]}). DEV ONLY."); return

    conn = await asyncpg.connect(dsn)
    try:
        user_id = await conn.fetchval("SELECT id FROM users WHERE email=$1", email)
        if not user_id:
            print(f"No user with email {email} in dev DB."); return
        print(f"Seeding listings for {email} (user_id={user_id})")

        records = load_rows()
        print(f"Loaded {len(records)} unique listings from CSVs")

        fresh = "--fresh" in sys.argv
        priority_vins = set(SYNTHETIC_BASELINES) | set(KNOWN_SOLD_CARGURUS)

        if fresh:
            # Exclude the DEFAULT mix (what we've been testing) + anything the DB
            # shows as already checked, so this is a genuinely different batch.
            default_sel = trim_mix(records, DEFAULT_TRIM, priority_vins)
            ex_urls = {r["url"] for r in default_sel}
            ex_vins = {r["vin"] for r in default_sel if r["vin"]}
            rows_checked = await conn.fetch(
                "SELECT listing_url, vin FROM listings WHERE user_id=$1 AND last_checked_at IS NOT NULL",
                user_id,
            )
            ex_urls |= {r["listing_url"] for r in rows_checked if r["listing_url"]}
            ex_vins |= {r["vin"] for r in rows_checked if r["vin"]}
            before = len(records)
            records = [r for r in records
                       if r["url"] not in ex_urls and (not r["vin"] or r["vin"] not in ex_vins)]
            print(f"--fresh: excluded {before - len(records)} (default mix + already-checked)")
            priority_vins = set(FRESH_SYNTHETIC_BASELINES) | set(FRESH_KNOWN_SOLD_CARGURUS)

        # Trim to a balanced mix unless --all is passed.
        trim = DEFAULT_TRIM
        if "--all" in sys.argv:
            trim = 0
        if "--limit" in sys.argv:
            trim = int(sys.argv[sys.argv.index("--limit") + 1])
        if trim:
            records = trim_mix(records, trim, priority_vins)
            by_src = {}
            for r in records:
                by_src[source_of(r["url"])] = by_src.get(source_of(r["url"]), 0) + 1
            print(f"Trimmed to {len(records)} (mix: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(by_src.items())) + ")")

        # Reset the slate so "Check Listings" sees EXACTLY the seeded set:
        # un-post every current listing for this dev user, then re-post the
        # selected ones below. (Dev test account only.)
        reset = await conn.execute(
            "UPDATE listings SET fb_posted=FALSE WHERE user_id=$1 AND fb_posted=TRUE",
            user_id,
        )
        print(f"Reset prior fb_posted flags: {reset}")

        now = datetime.utcnow()
        inserted = updated = synth = 0
        for r in records:
            baseline = ALL_BASELINES.get(r["vin"]) if r["vin"] else None
            if baseline:
                synth += 1
            # Find an existing row for this user by VIN (preferred) or URL.
            existing_id = None
            if r["vin"]:
                existing_id = await conn.fetchval(
                    "SELECT id FROM listings WHERE user_id=$1 AND vin=$2 LIMIT 1",
                    user_id, r["vin"])
            if existing_id is None:
                existing_id = await conn.fetchval(
                    "SELECT id FROM listings WHERE user_id=$1 AND listing_url=$2 LIMIT 1",
                    user_id, r["url"])

            if existing_id is not None:
                # Upsert (FK-safe — never delete; ad_events may reference the row).
                # Bump created_at to now so the seeded rows are the NEWEST — GET
                # /listings/ only returns the latest 20 by created_at, so stale
                # update timestamps would push the seeded set out of the window.
                await conn.execute(
                    """
                    UPDATE listings SET
                      year=$2, make=$3, model=$4, price=$5, listing_url=$6,
                      fb_posted=TRUE, fb_posted_at=$7, is_sold=FALSE,
                      sold_detected_at=NULL, last_checked_at=NULL,
                      created_at=$7, updated_at=$7
                    WHERE id=$1
                    """,
                    existing_id, r["year"], r["make"], r["model"],
                    baseline, r["url"], now,
                )
                updated += 1
            else:
                await conn.execute(
                    """
                    INSERT INTO listings
                      (user_id, vin, year, make, model, price, listing_url,
                       fb_posted, fb_posted_at, is_sold, last_checked_at,
                       created_at, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7, TRUE,$8, FALSE, NULL, $8,$8)
                    """,
                    user_id, r["vin"], r["year"], r["make"], r["model"],
                    baseline, r["url"], now,
                )
                inserted += 1

        print(f"Seeded {inserted + updated} listings (fb_posted=True, is_sold=False): "
              f"{inserted} new, {updated} updated")
        print(f"  - {synth} with a synthetic baseline price (price-change demo)")
        print(f"  - {inserted + updated - synth} with NULL baseline (first check adopts scraped price)")
        print("Done.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
