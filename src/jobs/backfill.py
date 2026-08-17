"""Review backfill — reconstruct an app's history from timestamped reviews.

This is what lets a newly discovered app be scored today instead of a week from now. Every
review carries a posting time, so one backfill yields an arrival curve reaching back before we
had ever heard of the app.

**Time-boxed, not count-boxed** (spec §8). Apple's ~20 requests/minute means a deep app costs
ten requests, and a fixed count would either waste the budget or blow the timeout. The job
works the queue until its deadline and stops cleanly; the remainder carries to the next run.
Decoupled from `daily` for the same reason — inside daily's 50 minutes it would starve
enrichment and scoring.

**`reviews_backfilled` is a one-way flag**, so a partial walk must never set it. If any page
fails, the app is left in the queue and retried whole (spec §6.3). A gap written today would
be permanent, because nothing ever revisits a completed app.
"""

from __future__ import annotations

import argparse
import sys
import time

from ..lib import db, log
from ..lib.http import Blocked, apple_fetcher
from ..stores.ios import reviews as ios_reviews


def run(conn, rl: log.RunLog, *, budget_seconds: int, limit: int) -> dict:
    fetcher = apple_fetcher()
    queue = db.backfill_queue(conn, "ios", limit)
    deadline = time.monotonic() + budget_seconds
    print(f"queue: {len(queue)} ios apps, budget {budget_seconds // 60} min")

    counts = {"apps": 0, "reviews": 0, "truncated": 0, "throttled": 0, "empty": 0}

    for i, row in enumerate(queue, 1):
        if time.monotonic() > deadline:
            print(f"  budget spent after {i - 1} apps — remainder carries to the next run")
            break

        app_id, sid = row["id"], row["store_app_id"]
        try:
            reviews, truncated = ios_reviews.fetch_all_reviews(fetcher, sid)
        except ios_reviews.Throttled as e:
            # Unknown, not zero. Leave the flag false so the whole app is retried.
            counts["throttled"] += 1
            print(f"  [{i}/{len(queue)}] THROTTLED {sid}: {e}")
            continue
        except Blocked:
            raise

        inserted = db.insert_review_events(conn, app_id, reviews)
        db.mark_backfilled(conn, app_id)
        conn.commit()

        counts["apps"] += 1
        counts["reviews"] += inserted
        if truncated:
            counts["truncated"] += 1
        if not reviews:
            # A genuine zero: the walk completed and Apple served a well-formed empty feed.
            # Normal for an app that is days old and has no *written* reviews yet.
            counts["empty"] += 1

        if i % 10 == 0 or i == len(queue):
            print(
                f"  [{i}/{len(queue)}] apps={counts['apps']} reviews={counts['reviews']} "
                f"truncated={counts['truncated']} throttled={counts['throttled']}"
            )
            rl.update(**counts, **fetcher.stats.as_dict())

    rl.update(**counts, **fetcher.stats.as_dict())
    print(f"→ {counts}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill iOS review history")
    ap.add_argument(
        "--budget-minutes",
        type=int,
        default=40,
        help="stop cleanly after this long, leaving the rest of the queue for the next run",
    )
    ap.add_argument("--limit", type=int, default=500, help="max apps to consider this run")
    args = ap.parse_args()

    with log.run("backfill") as rl:
        try:
            with db.connect() as conn:
                run(conn, rl, budget_seconds=args.budget_minutes * 60, limit=args.limit)
        except Blocked as e:
            print(f"\nBLOCKED: {e}")
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
