"""Apple RSS feeds — discovery and chart ranks.

This is the half of the system Play cannot match. Apple still publishes new-app feeds, and
all three variants work (verified 2026-08-17), so iOS apps are catchable on release day. Play
has no equivalent surface left: `NEW_FREE` is dead, and its best channel finds apps at a
median age of 115 days.

Two feed families, doing different jobs:

  new*    — discovery. Apps that just shipped.
  top*    — rank. An app's chart position is a scoring input (`rank_velocity`), and a late
            discovery channel in its own right.

Both cap at 100 entries per genre regardless of the `limit` parameter — `limit=200` still
returns 100.
"""

from __future__ import annotations

import json

from ...lib.http import Fetcher

FEED_URL = "https://itunes.apple.com/us/rss/{feed}/limit=100/genre={genre}/json"

NEW_FEEDS = ["newapplications", "newfreeapplications", "newpaidapplications"]
CHART_FEEDS = ["topfreeapplications", "toppaidapplications"]

# iOS app genres. 6014 (Games) is the volume outlier; the games subgenres 7001-7017 sit
# beneath it and are worth sweeping separately because 100 entries of "Games" is a thin
# slice of the largest category on the store.
GENRES = [
    6000,  # Business
    6001,  # Weather
    6002,  # Utilities
    6003,  # Travel
    6004,  # Sports
    6005,  # Social Networking
    6006,  # Reference
    6007,  # Productivity
    6008,  # Photo & Video
    6009,  # News
    6010,  # Navigation
    6011,  # Music
    6012,  # Lifestyle
    6013,  # Health & Fitness
    6015,  # Finance
    6016,  # Entertainment
    6017,  # Education
    6018,  # Books
    6020,  # Medical
    6023,  # Food & Drink
    6024,  # Shopping
]

GAME_GENRES = [
    7001,  # Action
    7002,  # Adventure
    7003,  # Casual
    7004,  # Board
    7005,  # Card
    7006,  # Casino
    7008,  # Family
    7009,  # Music
    7011,  # Puzzle
    7012,  # Racing
    7013,  # Role Playing
    7014,  # Simulation
    7015,  # Sports
    7016,  # Strategy
    7017,  # Trivia
]


def _entries(text: str | None) -> list[dict]:
    """Parse a legacy Apple RSS JSON feed. Tolerates the single-entry case, where Apple
    emits an object where it usually emits a list."""
    if not text:
        return []
    try:
        feed = json.loads(text).get("feed", {})
    except json.JSONDecodeError:
        return []
    entries = feed.get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    return entries if isinstance(entries, list) else []


def _track_id(entry: dict) -> str | None:
    return entry.get("id", {}).get("attributes", {}).get("im:id")


def _has_entries(text: str) -> bool:
    """Expectation predicate. An Apple feed that is empty *and* linkless is a throttle
    response, not an empty category — see guards.apple_feed_looks_throttled."""
    return len(_entries(text)) > 0


def fetch_feed(fetcher: Fetcher, feed: str, genre: int) -> list[dict]:
    text = fetcher.get(FEED_URL.format(feed=feed, genre=genre), expect=_has_entries, retries=1)
    return _entries(text)


def discover_new(
    fetcher: Fetcher,
    genres: list[int] | None = None,
    feeds: list[str] | None = None,
) -> list[tuple[str, str, None]]:
    """Sweep the new-app feeds. Returns (track_id, 'newapps_feed', None) triples.

    The third slot is the discovery install band, which Play's search results carry and Apple
    has no equivalent for. Kept for a uniform interface with the Play channels; the enrich
    queue sorts nulls last, so iOS apps are enriched after known-tiny Play apps. That is
    acceptable because iOS enrichment is ~200x cheaper per app (see lookup.py).
    """
    genres = genres if genres is not None else GENRES + GAME_GENRES
    feeds = feeds if feeds is not None else NEW_FEEDS
    found: set[str] = set()

    for feed in feeds:
        before = len(found)
        for genre in genres:
            for e in fetch_feed(fetcher, feed, genre):
                tid = _track_id(e)
                if tid:
                    found.add(tid)
        print(f"  {feed}: pool now {len(found)} (+{len(found) - before})")

    return [(tid, "newapps_feed", None) for tid in sorted(found)]


def fetch_chart_ranks(
    fetcher: Fetcher,
    genres: list[int] | None = None,
    feeds: list[str] | None = None,
) -> tuple[list[tuple[str, str, None]], dict[str, int]]:
    """Sweep chart feeds. Returns (discovery triples, {track_id: best_rank}).

    Rank is the *best* position across every chart the app appears in, because an app ranked
    #3 in a games subgenre and #150 overall is meaningfully a top-3 app in its niche. Feeding
    the worst rank instead would bury exactly the apps this system exists to find.
    """
    genres = genres if genres is not None else GENRES + GAME_GENRES
    feeds = feeds if feeds is not None else CHART_FEEDS
    best: dict[str, int] = {}

    for feed in feeds:
        for genre in genres:
            for position, e in enumerate(fetch_feed(fetcher, feed, genre), start=1):
                tid = _track_id(e)
                if not tid:
                    continue
                if tid not in best or position < best[tid]:
                    best[tid] = position
        print(f"  {feed}: {len(best)} ranked apps so far")

    return [(tid, "chart", None) for tid in sorted(best)], best
