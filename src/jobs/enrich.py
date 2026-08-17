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
from ..lib.guards import (
    NullRateTracker,
    ParserFailure,
    SuspectData,
    check_install_delta,
    classify_rating_drop,
)
from ..lib.http import AppNotFound, Blocked, apple_fetcher, play_fetcher
from ..stores.ios import lookup as ios_lookup
from ..stores.play import detail as play_detail


def enrich_play(conn, rl: log.RunLog, *, day: date, limit: int, via: str | None = None) -> dict:
    fetcher = play_fetcher()
    queue = db.enrich_queue(conn, "play", limit, via=via)
    print(f"queue: {len(queue)} play apps for {day.isoformat()}" + (f" (via={via})" if via else ""))

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


def enrich_ios(conn, rl: log.RunLog, *, day: date, limit: int) -> dict:
    """iOS enrichment via batched lookup.

    Economically the opposite of Play: 200 apps per request instead of one page per app, so
    the whole iOS catalogue can be snapshotted daily rather than worked through a queue.

    One important difference in what counts as a failure. On Play, a null install count beside
    populated ratings is parser drift. On iOS, an absent `userRatingCount` is *normal* — a
    brand-new app genuinely has no ratings yet, and those are exactly the apps this system
    exists to find. Treating that as drift would reject the most interesting rows.
    """
    fetcher = apple_fetcher()
    queue = db.enrich_queue(conn, "ios", limit)
    print(f"queue: {len(queue)} ios apps for {day.isoformat()} "
          f"({(len(queue) + 199) // 200} lookup batches)")

    by_sid = {r["store_app_id"]: r for r in queue}
    counts = {"ok": 0, "missing": 0, "relaunch": 0, "batch_fail": 0}

    for batch_no, chunk in enumerate(ios_lookup.batches(list(by_sid)), start=1):
        try:
            found, missing = ios_lookup.lookup_batch(fetcher, chunk)
        except RuntimeError as e:
            # Whole batch indeterminate. Do not delist 200 apps because one request failed.
            counts["batch_fail"] += 1
            print(f"  batch {batch_no}: FAILED — {e}")
            continue

        for sid, detail in found.items():
            row = by_sid[sid]
            app_id = row["id"]
            app_fields, metrics = ios_lookup.split_detail(detail)

            # Spec §6.6: iOS rating counts legitimately fall when a developer resets ratings
            # on a version release. That is a relaunch signal, not a parser bug — flag it so
            # `trust` can penalise it, and so the negative delta is explainable later.
            prev = db.last_snapshot_metric(conn, app_id, "rating_count")
            if classify_rating_drop(prev, metrics.get("rating_count")) == "relaunch":
                counts["relaunch"] += 1
                db.set_flags(conn, app_id, relaunch_suspect=True)
                print(f"  {sid} ({row.get('name')}): ratings fell {prev} → "
                      f"{metrics.get('rating_count')} — relaunch")

            db.upsert_app(
                conn,
                store="ios",
                store_app_id=sid,
                discovered_via=row.get("discovered_via") or "seed",
                **app_fields,
            )
            db.upsert_snapshot(conn, app_id, day, **metrics)
            db.mark_enriched(conn, app_id)
            counts["ok"] += 1

        for sid in missing:
            # Absent from the US storefront: delisted, pulled, or region-locked. The
            # three-strike rule in record_enrich_failure decides, not one absence.
            counts["missing"] += 1
            db.record_enrich_failure(conn, by_sid[sid]["id"])

        print(f"  batch {batch_no}: {len(found)} ok, {len(missing)} missing")
        conn.commit()
        rl.update(ios=counts, ios_http=fetcher.stats.as_dict())

    rl.update(ios=counts, day=day.isoformat(), ios_http=fetcher.stats.as_dict())
    print(f"→ ios {counts}")
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
    ap.add_argument("--limit", type=int, default=400, help="Play apps to enrich this run")
    ap.add_argument(
        "--ios-limit",
        type=int,
        default=2000,
        help="iOS apps to enrich this run. Much higher than the Play limit on purpose: "
        "lookup returns 200 apps per request, so 2000 apps costs 10 requests.",
    )
    ap.add_argument("--stores", default="play,ios", help="comma-separated subset of play,ios")
    ap.add_argument(
        "--via",
        help="restrict to one discovery channel (chart|search|developer). Diagnostic: "
        "use it to measure a channel's discovery latency before trusting its budget.",
    )
    args = ap.parse_args()
    day = parse_day(args.day)

    stores = {s.strip() for s in args.stores.split(",") if s.strip()}

    with log.run("enrich") as rl:
        try:
            with db.connect() as conn:
                if "play" in stores:
                    enrich_play(conn, rl, day=day, limit=args.limit, via=args.via)
                if "ios" in stores:
                    enrich_ios(conn, rl, day=day, limit=args.ios_limit)
        except Blocked as e:
            print(f"\nBLOCKED: {e}")
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
