"""Enrichment job — fetch detail for queued apps, write app fields + one snapshot row.

Two things here are load-bearing:

**The day is stamped from intent, not from the clock** (spec §8). Actions cron fires hours
late; `--day` defaults to today in UTC but a late or replayed run can be told which day it
covers, so a re-run overwrites cleanly instead of smearing one day's data across two.

**A row can fail without failing the run, but the batch can still fail the run.** A single
parser miss is normal (apps get delisted mid-crawl). A *rate* of parser misses means Google
changed the page and every number we are writing is suspect — that must stop the run before
it writes a day of nulls. `NullRateTracker` draws that line at 5%.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from ..lib import db, log
from ..lib.guards import NullRateTracker, ParserFailure, SuspectData, check_install_delta
from ..lib.http import AppNotFound, Blocked, play_fetcher
from ..stores.play import detail as play_detail


def enrich_play(conn, rl: log.RunLog, *, day: date, limit: int) -> dict:
    fetcher = play_fetcher()
    queue = db.enrich_queue(conn, "play", limit)
    print(f"queue: {len(queue)} play apps for {day.isoformat()}")

    tracker = NullRateTracker("play detail")
    counts = {"ok": 0, "delisted": 0, "parser_fail": 0, "suspect": 0, "error": 0}

    for i, row in enumerate(queue, 1):
        app_id, store_app_id = row["id"], row["store_app_id"]
        try:
            detail = play_detail.fetch_detail(fetcher, store_app_id)
        except AppNotFound:
            counts["delisted"] += 1
            tracker.record(True)  # a delisted app is a real answer, not a parser miss
            db.record_enrich_failure(conn, app_id, delist=True)
            print(f"  [{i}/{len(queue)}] {store_app_id}: delisted")
            continue
        except ParserFailure as e:
            counts["parser_fail"] += 1
            tracker.record(False, str(e))
            db.record_enrich_failure(conn, app_id)
            print(f"  [{i}/{len(queue)}] PARSER: {e}")
            tracker.check()
            continue
        except Blocked:
            raise
        except Exception as e:
            counts["error"] += 1
            tracker.record(False, f"{store_app_id}: {type(e).__name__}")
            db.record_enrich_failure(conn, app_id)
            print(f"  [{i}/{len(queue)}] ERROR {type(e).__name__}: {e}")
            tracker.check()
            continue

        app_fields, metrics = play_detail.split_detail(detail)

        # Impossible-delta guard (spec §6.6): cumulative installs cannot fall. If they did,
        # we mis-parsed one of the two readings, so refuse the row rather than record a crash.
        prev = db.last_snapshot_metric(conn, app_id, "install_exact")
        try:
            check_install_delta(prev, metrics.get("install_exact"), store_app_id)
        except SuspectData as e:
            counts["suspect"] += 1
            tracker.record(False, str(e))
            db.record_enrich_failure(conn, app_id)
            print(f"  [{i}/{len(queue)}] SUSPECT: {e}")
            tracker.check()
            continue

        db.upsert_app(
            conn,
            store="play",
            store_app_id=store_app_id,
            discovered_via=row.get("discovered_via") or "seed",
            **app_fields,
        )
        db.upsert_snapshot(conn, app_id, day, **metrics)
        db.mark_enriched(conn, app_id)
        tracker.record(True)
        counts["ok"] += 1

        if i % 25 == 0 or i == len(queue):
            print(
                f"  [{i}/{len(queue)}] ok={counts['ok']} delisted={counts['delisted']} "
                f"parser_fail={counts['parser_fail']} null_rate={tracker.rate:.1%}"
            )
            rl.update(play=counts, null_rate=round(tracker.rate, 4), **fetcher.stats.as_dict())

    # Final gate: if the batch as a whole looks like parser drift, fail the run.
    tracker.check()

    rl.update(
        play=counts,
        null_rate=round(tracker.rate, 4),
        day=day.isoformat(),
        **fetcher.stats.as_dict(),
    )
    print(f"→ {counts} over {tracker.total} apps; null rate {tracker.rate:.1%}")
    return counts


def parse_day(value: str | None) -> date:
    if not value:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(value)


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich queued apps and snapshot them")
    ap.add_argument(
        "--day",
        help="UTC date this run covers (YYYY-MM-DD). Defaults to today; set it explicitly "
        "when replaying a late or missed run so the snapshot lands on the right day.",
    )
    ap.add_argument("--limit", type=int, default=400, help="apps to enrich this run")
    args = ap.parse_args()
    day = parse_day(args.day)

    with log.run("enrich") as rl:
        try:
            with db.connect() as conn:
                enrich_play(conn, rl, day=day, limit=args.limit)
        except Blocked as e:
            print(f"\nBLOCKED: {e}")
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
