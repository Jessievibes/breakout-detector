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

from ..lib import db, log
from ..lib.http import Blocked, play_fetcher
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover new app ids")
    ap.add_argument(
        "--channels",
        default="chart,search,developer",
        help="comma-separated subset of chart,search,developer",
    )
    ap.add_argument("--dev-seeds", type=int, default=40, help="developer pages to crawl (D2)")
    args = ap.parse_args()
    channels = {c.strip() for c in args.channels.split(",") if c.strip()}

    with log.run("discover") as rl:
        try:
            with db.connect() as conn:
                run_play(conn, rl, channels=channels, dev_seeds=args.dev_seeds)
        except Blocked as e:
            # Spec §6.4: the IP is burning. Die loudly; the discovered ids that were
            # already committed stay, and the run goes red.
            print(f"\nBLOCKED: {e}")
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
