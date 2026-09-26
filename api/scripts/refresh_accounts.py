"""
Merges freshly-fetched experience.com data into data/accounts.json.

This does NOT fetch anything itself — pulling live review data off
experience.com's public pages needs an agent with WebSearch/WebFetch to parse
unstructured HTML (that's how the accounts already in the fixture were
sourced). This script is the deterministic second half: given already-fetched,
already-normalized data, it merges it in consistently and reuses main.py's
own compute_health_score so scoring never drifts between the app and the
refresh path.

Usage:
    python3 src/scripts/refresh_accounts.py path/to/fetched.json   # from the repo root

Input JSON shape:
{
  "updates": [
    {
      "source_url": "https://www.experience.com/reviews/...",   // must match an
                                                                   // existing account's source_url
      "reviews": [
        {"rating": 5, "text": "...", "date": "YYYY-MM-DD",
         "responded": true, "response_date": "YYYY-MM-DD" | null,
         "response_text": "..." | null},   // the actual reply text, when responded
        ...
      ]
    }
  ],
  "new_accounts": [
    {
      "business_name": "...", "category": "...", "account_type": "firm" | "individual",
      "address": "...", "phone": "...", "source_url": "...",
      "reviews": [ ... same shape as above ... ]
    }
  ]
}

Ratings must already be integers 1-5 and dates already ISO (YYYY-MM-DD) — do
that normalization when preparing the fetched data, the same way it was done
by hand when this fixture was first built.

For each matched update:
  - the account's CURRENT health score is computed and appended as a new
    weekly_health_history entry (oldest dropped once there are more than 4) —
    this is how real week-over-week trend accumulates over repeated refreshes,
    instead of the flat series a single one-off snapshot has to start with.
  - reviews are then replaced with the freshly-fetched set.

New accounts are only added if their source_url isn't already present
(dedup), and start with a flat 4-week history since there's no prior data yet.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from main import ANCHOR_DATE, HEALTH_SCORE_FORMULA_VERSION, compute_health_score  # noqa: E402

DATA_PATH = BASE_DIR / "data" / "accounts.json"
MAX_HISTORY_WEEKS = 4


def _reviews_from_input(raw_reviews: list[dict]) -> list[dict]:
    return [
        {
            "id": f"r{i}",
            "rating": r["rating"],
            "text": r["text"],
            "date": r["date"],
            "responded": r["responded"],
            "response_date": r.get("response_date"),
            "response_text": r.get("response_text"),
        }
        for i, r in enumerate(raw_reviews, start=1)
    ]


def _shifted_history(account: dict) -> list[dict]:
    """Append the account's score computed from its *current* (pre-update)
    reviews as a new history point, dropping the oldest once there are more
    than MAX_HISTORY_WEEKS. Stamped with the CURRENT formula version, so a
    methodology change is never silently mistaken for a real trend — see
    compute_churn_risk's version-aware streak counting in main.py."""
    current_score = compute_health_score(account, ANCHOR_DATE)["health_score"]
    history = list(account["weekly_health_history"])
    last_week_ending = history[-1]["week_ending"] if history else ANCHOR_DATE.isoformat()
    from datetime import date as _date
    next_week_ending = (_date.fromisoformat(last_week_ending) + timedelta(days=7)).isoformat()
    history.append({
        "week_ending": next_week_ending,
        "score": current_score,
        "formula_version": HEALTH_SCORE_FORMULA_VERSION,
    })
    return history[-MAX_HISTORY_WEEKS:]


def merge(fetched: dict) -> dict:
    data = json.loads(DATA_PATH.read_text())
    accounts = data["accounts"]
    by_source_url = {a.get("source_url"): a for a in accounts if a.get("source_url")}

    updated_count = 0
    for update in fetched.get("updates", []):
        account = by_source_url.get(update["source_url"])
        if account is None:
            print(f"  SKIP (no matching account): {update['source_url']}")
            continue
        account["weekly_health_history"] = _shifted_history(account)
        account["reviews"] = _reviews_from_input(update["reviews"])
        updated_count += 1

    existing_urls = {a.get("source_url") for a in accounts if a.get("source_url")}
    next_idx = len(accounts) + 1
    added_count = 0
    for new_acc in fetched.get("new_accounts", []):
        if new_acc["source_url"] in existing_urls:
            print(f"  SKIP (already present): {new_acc['source_url']}")
            continue
        reviews = _reviews_from_input(new_acc["reviews"])
        account = {
            "id": f"acc_{next_idx:03d}",
            "business_name": new_acc["business_name"],
            "category": new_acc["category"],
            "account_type": new_acc.get("account_type", "individual"),
            "source_url": new_acc["source_url"],
            "reviews": reviews,
            "listings": [{
                "directory": "Experience.com",
                "name": new_acc["business_name"],
                "address": new_acc.get("address", ""),
                "phone": new_acc.get("phone", ""),
            }],
        }
        score = compute_health_score(account, ANCHOR_DATE)["health_score"]
        week_endings = [
            (ANCHOR_DATE - timedelta(days=7 * i)).isoformat() for i in range(3, -1, -1)
        ]
        account["weekly_health_history"] = [
            {"week_ending": w, "score": score, "formula_version": HEALTH_SCORE_FORMULA_VERSION}
            for w in week_endings
        ]
        accounts.append(account)
        existing_urls.add(new_acc["source_url"])
        next_idx += 1
        added_count += 1

    DATA_PATH.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Updated {updated_count} accounts, added {added_count} new accounts. "
          f"Total now: {len(accounts)}.")
    print("Call POST /api/admin/reload-data (or restart the server) to pick this up.")
    return data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    fetched_path = Path(sys.argv[1])
    merge(json.loads(fetched_path.read_text()))
