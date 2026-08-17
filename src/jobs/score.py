"""Scoring job — invoke the SQL scoring function after enrichment.

Thin by design. The scoring logic lives in `sql/004_scoring.sql` because it is set-relative:
z-scores need the whole cohort at once, and pulling thousands of rows into Python to do what
`percentile_cont` and `stddev_samp` already do would be slower and easier to get wrong.

This job exists so that Actions owns the ordering — scoring must run after enrichment, and
nothing inside Postgres can tell whether today's scrape actually succeeded.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from ..lib import db, log


def run(conn, rl: log.RunLog, *, day: date) -> dict:
    with conn.cursor() as cur:
        cur.execute("select compute_breakout_scores(%s) as n", [day])
        scored = cur.fetchone()["n"]

        # Coverage, not just a count: a run that scores 12 of 3,000 eligible apps is
        # technically successful and practically broken.
        cur.execute(
            """
            select count(*) filter (where a.released > %s - 120 and a.delisted = false
                                      and a.released is not null)          as eligible,
                   count(bs.app_id)                                        as scored,
                   count(*) filter (where (bs.components->>'cold_start')::boolean) as cold_start,
                   count(*) filter (where a.clone_suspect)                 as clone_suspect
              from app a
              left join breakout_score bs on bs.app_id = a.id and bs.scored_on = %s
            """,
            [day, day],
        )
        cov = cur.fetchone()

        cur.execute(
            """
            select a.store, count(*) as n, round(avg(bs.score), 4) as avg_score,
                   round(max(bs.score), 4) as max_score
              from breakout_score bs join app a on a.id = bs.app_id
             where bs.scored_on = %s group by a.store
            """,
            [day],
        )
        per_store = {r["store"]: dict(r) for r in cur.fetchall()}

    stats = {
        "scored": scored,
        "eligible": cov["eligible"],
        "cold_start": cov["cold_start"],
        "clone_suspect": cov["clone_suspect"],
        "per_store": {k: {"n": v["n"], "max": float(v["max_score"] or 0)} for k, v in per_store.items()},
        "day": day.isoformat(),
    }
    rl.update(**stats)
    print(f"→ scored {scored} of {cov['eligible']} eligible apps")
    for store, v in per_store.items():
        print(f"    {store}: {v['n']} scored, max {v['max_score']}, avg {v['avg_score']}")
    if cov["eligible"] and scored / cov["eligible"] < 0.5:
        # Not fatal — early on, most apps genuinely have too little data. But it should be
        # visible rather than buried, because it is also what a broken join looks like.
        print(f"  ! only {scored / cov['eligible']:.0%} of eligible apps could be scored")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute breakout scores")
    ap.add_argument("--day", help="UTC day to score (YYYY-MM-DD); defaults to today")
    args = ap.parse_args()
    day = date.fromisoformat(args.day) if args.day else datetime.now(timezone.utc).date()

    with log.run("score") as rl:
        with db.connect() as conn:
            run(conn, rl, day=day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
