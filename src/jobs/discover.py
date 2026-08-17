"""Discovery job — register app ids that exist. Enrichment fills in everything else.

Keeping discovery this thin is deliberate: a channel's only job is to produce ids, so a
channel failing cannot corrupt app metadata, and adding a channel means adding a function
that returns pairs.

Channel yield is logged per run because discovery is the system's ceiling. The number that
matters is not "how many ids" but `median(first_seen − released)` per channel — whether a
channel finds apps *early* or merely finds apps. Review it after two weeks and kill
channels that only surface old apps (spec §5).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from ..lib import db, log
from ..lib.http import Blocked, apple_fetcher, play_fetcher
from ..stores.ios import feeds as ios_feeds
from ..stores.play import discover as play_discover


def run_play(conn, rl: log.RunLog, *, channels: set[str], dev_seeds: int) -> dict:
    fetcher = play_fetcher()
    per_channel: dict[str, dict] = {}
    total_new = 0

    def register(pairs: list[tuple[str, str, int | None]], channel: str) -> None:
        nonlocal total_new
        new = db.insert_apps_bulk(conn, "play", pairs)
        # Commit per channel. If a later channel trips the IP guard, the work already done
        # survives instead of rolling back — the first live run lost ~1000 chart ids that way.
        conn.commit()
        per_channel[channel] = {"found": len(pairs), "new": new}
        total_new += new
        print(f"  → {channel}: {len(pairs)} ids, {new} new")

    if "chart" in channels:
        print("[D4] charts + category pages")
        register(play_discover.chart_scrape(fetcher), "chart")

    if "search" in channels:
        print("[D1] keyword search sweep")
        register(play_discover.search_sweep(fetcher), "search")

    if "developer" in channels:
        print("[D2] developer pages")
        seeds = db.developer_crawl_seeds(conn, dev_seeds)
        if seeds:
            register(play_discover.developer_crawl(fetcher, seeds), "developer")
        else:
            # Expected on the very first run: D2 compounds from apps we already know, so it
            # has nothing to crawl until enrich has populated developer names.
            print("  no developer seeds yet — D2 bootstraps after the first enrich run")
            per_channel["developer"] = {"found": 0, "new": 0, "note": "no seeds yet"}

    rl.update(play_channels=per_channel, play_new=total_new, **fetcher.stats.as_dict())
    return per_channel


def run_ios(conn, rl: log.RunLog, *, day: date) -> dict:
    """iOS discovery — the only true day-zero channel in the system.

    Apple still publishes new-app feeds and all three variants work, so apps are catchable on
    release day. Chart ranks are written here rather than in enrich because this job is
    already paying for the chart fetches, and `upsert_ranks` lets rank and metrics land in
    either order.
    """
    fetcher = apple_fetcher()
    per_channel: dict[str, dict] = {}
    total_new = 0

    print("[iOS] new-app feeds")
    new_apps = ios_feeds.discover_new(fetcher)
    new_count = db.insert_apps_bulk(conn, "ios", new_apps)
    conn.commit()
    per_channel["newapps_feed"] = {"found": len(new_apps), "new": new_count}
    total_new += new_count
    print(f"  → newapps_feed: {len(new_apps)} ids, {new_count} new")

    print("[iOS] chart feeds (discovery + rank)")
    chart_apps, ranks, chart_counts = ios_feeds.fetch_chart_ranks(fetcher)
    chart_count = db.insert_apps_bulk(conn, "ios", chart_apps)
    conn.commit()
    ranked = db.upsert_ranks(conn, "ios", ranks, day, chart_counts)
    conn.commit()
    per_channel["chart"] = {"found": len(chart_apps), "new": chart_count, "ranked": ranked}
    total_new += chart_count
    print(f"  → chart: {len(chart_apps)} ids, {chart_count} new, {ranked} ranks written")

    rl.update(ios_channels=per_channel, ios_new=total_new, ios_http=fetcher.stats.as_dict())
    return per_channel


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover new app ids")
    ap.add_argument(
        "--stores", default="play,ios", help="comma-separated subset of play,ios"
    )
    ap.add_argument(
        "--channels",
        default="chart,search,developer",
        help="Play channels: comma-separated subset of chart,search,developer",
    )
    ap.add_argument("--dev-seeds", type=int, default=40, help="developer pages to crawl (D2)")
    ap.add_argument("--day", help="UTC day this run covers (YYYY-MM-DD); defaults to today")
    args = ap.parse_args()

    channels = {c.strip() for c in args.channels.split(",") if c.strip()}
    stores = {s.strip() for s in args.stores.split(",") if s.strip()}
    day = date.fromisoformat(args.day) if args.day else datetime.now(timezone.utc).date()

    with log.run("discover") as rl:
        try:
            with db.connect() as conn:
                if "play" in stores:
                    run_play(conn, rl, channels=channels, dev_seeds=args.dev_seeds)
                if "ios" in stores:
                    run_ios(conn, rl, day=day)
        except Blocked as e:
            # Spec §6.4: the IP is burning. Die loudly; the discovered ids that were
            # already committed stay, and the run goes red.
            print(f"\nBLOCKED: {e}")
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
