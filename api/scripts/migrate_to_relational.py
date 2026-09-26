"""
One-off migration for the XMP-stack rebuild: pushes data/accounts.json into
the new relational tables (accounts, reviews, listings,
weekly_health_history) instead of the single jsonb `accounts` table, and
creates one demo subscriber login mapped to exactly one account via
account_owners — so logging in as that subscriber and hitting RLS proves
"a subscriber only ever sees their own profile."

Run the SQL in supabase/xmp_schema.sql first (Supabase SQL Editor), then:
    python3 src/scripts/migrate_to_relational.py [--demo-account acc_001]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent
DATA_PATH = BASE_DIR / "data" / "accounts.json"

load_dotenv(REPO_ROOT / ".env")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

DEMO_EMAIL = "demo@profilewatch.dev"
DEMO_PASSWORD = "profilewatch-demo-2026"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-account", default="acc_001", help="Account id the demo login owns")
    args = parser.parse_args()

    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (in .env or the environment).")
    if not DATA_PATH.exists():
        sys.exit(f"{DATA_PATH} not found — nothing to migrate.")

    accounts = json.loads(DATA_PATH.read_text())["accounts"]
    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    account_rows, review_rows, listing_rows, history_rows = [], [], [], []
    for a in accounts:
        account_rows.append({
            "id": a["id"],
            "business_name": a["business_name"],
            "category": a.get("category"),
            "account_type": a.get("account_type", "individual"),
            "source_url": a.get("source_url"),
        })
        for r in a["reviews"]:
            review_rows.append({**r, "account_id": a["id"]})
        for l in a["listings"]:
            listing_rows.append({**l, "account_id": a["id"]})
        for h in a["weekly_health_history"]:
            history_rows.append({**h, "account_id": a["id"]})

    client.table("xmp_accounts").upsert(account_rows).execute()
    client.table("xmp_reviews").upsert(review_rows, on_conflict="account_id,id").execute()
    # Listings have no natural key in the source data, so clear and re-insert
    # per account rather than upsert, to stay idempotent across re-runs.
    for a in accounts:
        client.table("xmp_listings").delete().eq("account_id", a["id"]).execute()
    client.table("xmp_listings").insert(listing_rows).execute()
    client.table("xmp_weekly_health_history").upsert(history_rows, on_conflict="account_id,week_ending").execute()

    print(f"Migrated {len(account_rows)} accounts, {len(review_rows)} reviews, "
          f"{len(listing_rows)} listings, {len(history_rows)} health-history points.")

    # Demo subscriber: create (or reuse) an auth user and link it to one
    # account, so signing in as this user only ever sees that account.
    demo_account_id = args.demo_account
    if not any(a["id"] == demo_account_id for a in accounts):
        sys.exit(f"No account with id {demo_account_id!r} — pick a real id from data/accounts.json.")

    try:
        created = client.auth.admin.create_user({
            "email": DEMO_EMAIL,
            "password": DEMO_PASSWORD,
            "email_confirm": True,
        })
        user_id = created.user.id
        print(f"Created demo auth user {DEMO_EMAIL}.")
    except Exception:
        existing = client.auth.admin.list_users()
        match = next((u for u in existing if u.email == DEMO_EMAIL), None)
        if not match:
            raise
        user_id = match.id
        print(f"Demo auth user {DEMO_EMAIL} already existed, reusing it.")

    client.table("xmp_account_owners").upsert({"account_id": demo_account_id, "user_id": user_id}).execute()
    print(f"Linked {DEMO_EMAIL} to account {demo_account_id!r}.")
    print(f"\nDemo login — email: {DEMO_EMAIL}  password: {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
