"""Apple RSS feeds — discovery and chart ranks.

**The chart feeds are the day-zero channel here, not the new-app feeds.** That is the
opposite of what the name suggests, and it was only visible from real release dates:

    channel        apps   under 120d   median age   youngest
    chart          6164    381 ( 6%)      2758d        0 days
    newapps_feed    208    208 (100%)       41d       41 days

`newapplications` is **frozen**. Of the 208 apps it returns, 206 were released on
2026-07-07 and the whole feed spans three days in early July — a stale snapshot roughly six
weeks old. Worse, it advertises a `feed.updated` timestamp regenerated on every request, so
it looks live while serving fixed content. There is no cheap fetch-time staleness signal;
the only detector is comparing release dates after enrichment, which is exactly the
`median(first_seen − released)` KPI the spec already asks for.

The chart feeds, by contrast, carry apps released *today*, often at high rank — new iOS
releases chart immediately (a 0-day-old game sat at #5 in Games). That makes charts behave
completely differently across the two stores: on Play, charts find apps with a median age of
2,005 days, because charting there means an app already broke out.

Two feed families, and they disagree about `genre`:

  new*    — genre is IGNORED. Every value returns the same global 100 apps (verified across
            6014, 6002, 6015, 6013 and 36). One request each; never sweep.
  top*    — genre is HONOURED. Games and Finance share 0 of 100 entries. Sweep these.

Both cap at 100 entries regardless of the `limit` parameter — `limit=200` returns 100.

The new-app feeds are kept because they cost three requests total and may thaw, but they are
demoted: charts are what actually finds new iOS apps today.
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


ALL_APPS_GENRE = 36  # Apple's "all apps" pseudo-genre


def discover_new(fetcher: Fetcher, feeds: list[str] | None = None) -> list[tuple[str, str, None]]:
    """Fetch the new-app feeds. Returns (track_id, 'newapps_feed', None) triples.

    **One request per feed, deliberately — the genre parameter is ignored here.** Verified
    2026-08-17: `newapplications` returns a byte-identical set of the same 100 apps for
    genre=6014, 6002, 6015, 6013 and 36. Sweeping 36 genres fetched the same 100 apps 36
    times over, at a cost of 108 requests for what 3 requests deliver.

    Note the asymmetry inside the same RSS family: chart feeds *do* honour genre (Games and
    Finance share 0 of 100 entries), which is why `fetch_chart_ranks` still sweeps them.

    The practical consequence is a much narrower firehose than the spec assumed. iOS day-zero
    discovery is roughly 200 apps/day total — 100 from `newapplications`, plus about another
    100 distinct paid apps from `newpaidapplications`; `newfreeapplications` overlaps the
    first almost entirely. That is still the only genuine release-day channel in the system,
    but it is a stream, not a flood.

    The third tuple slot is the discovery install band, which Play's search results carry and
    Apple has no equivalent for. Kept for a uniform interface; the enrich queue sorts nulls
    last, which is fine because iOS enrichment is ~200x cheaper per app (see lookup.py).
    """
    feeds = feeds if feeds is not None else NEW_FEEDS
    found: set[str] = set()

    for feed in feeds:
        before = len(found)
        for e in fetch_feed(fetcher, feed, ALL_APPS_GENRE):
            tid = _track_id(e)
            if tid:
                found.add(tid)
        print(f"  {feed}: pool now {len(found)} (+{len(found) - before})")

    return [(tid, "newapps_feed", None) for tid in sorted(found)]


def fetch_chart_ranks(
    fetcher: Fetcher,
    genres: list[int] | None = None,
    feeds: list[str] | None = None,
) -> tuple[list[tuple[str, str, None]], dict[str, int], dict[str, int]]:
    """Sweep chart feeds. Returns (discovery triples, {id: best_rank}, {id: chart_count}).

    Rank is the *best* position across every chart the app appears in, because an app ranked
    #3 in a games subgenre and #150 overall is meaningfully a top-3 app in its niche. Feeding
    the worst rank instead would bury exactly the apps this system exists to find.
    """
    genres = genres if genres is not None else GENRES + GAME_GENRES
    feeds = feeds if feeds is not None else CHART_FEEDS
    best: dict[str, int] = {}
    appearances: dict[str, int] = {}

    for feed in feeds:
        for genre in genres:
            for position, e in enumerate(fetch_feed(fetcher, feed, genre), start=1):
                tid = _track_id(e)
                if not tid:
                    continue
                if tid not in best or position < best[tid]:
                    best[tid] = position
                # Breadth, where best_rank is depth: #40 across five categories is a wider
                # phenomenon than #3 in one niche, and the sweep already sees every position
                # before collapsing them.
                appearances[tid] = appearances.get(tid, 0) + 1
        print(f"  {feed}: {len(best)} ranked apps so far")

    return [(tid, "chart", None) for tid in sorted(best)], best, appearances
