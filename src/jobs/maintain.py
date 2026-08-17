"""Maintenance job — twin matching and snapshot retention.

Both are set-based SQL and neither touches the network, so they are cheap and safe to run at
the end of the daily pipeline. Kept separate from `score` because a failure here should not
block a ranking: stale twins and un-rolled snapshots are inconveniences, a missing score is
the product not working.
"""

from __future__ import annotations

import argparse
import sys

from ..lib import db, log


def run(conn, rl: log.RunLog, *, cutoff_days: int, min_similarity: float) -> dict:
    with conn.cursor() as cur:
        cur.execute("select match_twins(%s) as n", [min_similarity])
        matched = cur.fetchone()["n"]

        cur.execute(
            """
            select count(*) filter (where twin_app_id is not null)          as twinned,
                   count(*) filter (where store = 'ios')                    as ios_total,
                   count(*) filter (where store = 'ios' and twin_app_id is not null) as ios_twinned,
                   round(avg(twin_confidence) filter (where twin_app_id is not null), 3) as avg_conf
              from app where delisted = false
            """
        )
        t = cur.fetchone()

        cur.execute("select roll_up_snapshots(%s) as n", [cutoff_days])
        rolled = cur.fetchone()["n"]

        cur.execute("select count(*) as n from snapshot")
        remaining = cur.fetchone()["n"]

    stats = {
        "twins_updated": matched,
        "twins_total": t["twinned"],
        "ios_twin_rate": round((t["ios_twinned"] or 0) / t["ios_total"], 4) if t["ios_total"] else 0,
        "twin_avg_confidence": float(t["avg_conf"] or 0),
        "weeks_rolled": rolled,
        "snapshot_rows": remaining,
    }
    rl.update(**stats)
    print(f"→ twins: {matched} updated, {t['twinned']} total "
          f"({stats['ios_twin_rate']:.1%} of iOS apps, avg confidence {stats['twin_avg_confidence']})")
    print(f"→ retention: {rolled} week-rows written, {remaining} daily snapshot rows remain")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Twin matching and snapshot retention")
    ap.add_argument("--cutoff-days", type=int, default=60,
                    help="fold daily snapshots older than this into weekly rollups")
    ap.add_argument("--min-similarity", type=float, default=0.55,
                    help="trigram threshold for fuzzy name matching across stores")
    args = ap.parse_args()

    with log.run("maintain") as rl:
        with db.connect() as conn:
            run(conn, rl, cutoff_days=args.cutoff_days, min_similarity=args.min_similarity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
