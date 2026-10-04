"""
Prod cleanup for orphaned outro_videos rows (bug #2 fix follow-up).

Outros 12, 14, and 18 had their S3 files deleted by earlier failed hard-delete
attempts, but their DB rows survived (ForeignKeyViolationError stopped the
DELETE before the COMMIT).  This script:

  1. Soft-deletes rows 12, 14, 18 (sets deleted_at = NOW()).
  2. Checks every other non-deleted outro_videos row for a missing S3 object
     and REPORTS any orphans found — does NOT soft-delete them automatically.

Run on prod via EC2 after deploying the soft-delete migration:
    /home/ubuntu/orbitads/venv/bin/python scripts/cleanup_orphaned_outros.py

Safe to re-run: already-soft-deleted rows are skipped, S3 HEAD checks are
read-only.
"""
import asyncio
import os

import asyncpg
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

KNOWN_ORPHAN_IDS = [12, 14, 18]


async def run():
    db_url = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
    bucket  = os.getenv("S3_BUCKET_NAME", "")
    region  = "us-east-2"

    s3 = boto3.client(
        "s3",
        aws_access_key_id     = os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name           = region,
    )

    conn = await asyncpg.connect(db_url)
    try:
        # ── Step 1: soft-delete the known orphans ──────────────────
        print(f"Soft-deleting known orphan IDs: {KNOWN_ORPHAN_IDS}")
        result = await conn.execute(
            """
            UPDATE outro_videos
               SET deleted_at = NOW()
             WHERE id = ANY($1::int[])
               AND deleted_at IS NULL
            """,
            KNOWN_ORPHAN_IDS,
        )
        print(f"  → {result.split()[-1]} row(s) updated")

        # ── Step 2: check all remaining active rows for missing S3 ─
        rows = await conn.fetch(
            """
            SELECT id, user_id, name, s3_key
              FROM outro_videos
             WHERE deleted_at IS NULL
               AND id != ALL($1::int[])
             ORDER BY id
            """,
            KNOWN_ORPHAN_IDS,
        )
        print(f"\nChecking {len(rows)} active outro row(s) for missing S3 objects...")

        orphans_found = []
        for row in rows:
            try:
                s3.head_object(Bucket=bucket, Key=row["s3_key"])
                print(f"  id={row['id']} user={row['user_id']} name={row['name']!r} — OK")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in ("404", "NoSuchKey"):
                    orphans_found.append(dict(row))
                    print(f"  id={row['id']} user={row['user_id']} name={row['name']!r} — MISSING (S3 key: {row['s3_key']})")
                else:
                    print(f"  id={row['id']} — S3 error {code}: {e}")

        if orphans_found:
            print(f"\n⚠️  {len(orphans_found)} additional orphan(s) found with missing S3 objects:")
            for o in orphans_found:
                print(f"    id={o['id']} user_id={o['user_id']} name={o['name']!r} s3_key={o['s3_key']}")
            print("These were NOT soft-deleted. Confirm with the user before acting.")
        else:
            print("\nAll remaining active outro rows have their S3 files intact.")

    finally:
        await conn.close()


asyncio.run(run())
