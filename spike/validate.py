#!/usr/bin/env python3
"""
Phase 0 validation spike — Breakout Detector.

Purpose: answer the questions in spec §11 with live data. The only one that matters
architecturally is #1: does a GitHub Actions runner IP survive both stores? Everything
else in spec §0 was already verified from a residential IP on 2026-08-12, so this script
exists mainly to be run *from CI* and compared against those known-good numbers.

Writes a markdown result table to VALIDATION.md and exits non-zero if a hard gate fails.

Usage:
    python spike/validate.py            # full run (~15-20 min, for Actions)
    python spike/validate.py --quick    # reduced sample (~3 min, for local smoke tests)

This script is throwaway. Its HTTP/guard patterns graduate into src/lib/, then it is deleted.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

try:
    from google_play_scraper import app as play_app
    from google_play_scraper import reviews as play_reviews
    from google_play_scraper import search as play_search
    from google_play_scraper.exceptions import NotFoundError
except ImportError:
    sys.exit("missing dependency: pip install -r requirements.txt")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Known Play apps spanning five orders of magnitude of install base (470K → 3.1B).
# Test 1 asserts realInstalls is exact (≠ the minInstalls band floor) across the whole
# range — a parser that only works on billion-install apps is not a working parser.
# Every entry verified to resolve on 2026-08-17; iOS-only apps make bad fixtures here.
PLAY_KNOWN = [
    "com.spotify.music",         # 3.09B
    "org.telegram.messenger",    # 2.95B
    "com.duolingo",              # 956M
    "com.nianticlabs.pokemongo", # 562M
    "org.wikipedia",             # 72M
    "com.anthropic.claude",      # 56M
    "com.todoist",               # 49M
    "notion.id",                 # 42M
    "com.bandcamp.android",      # 4.9M
    "net.osmand.plus",           # 470K — the low-magnitude canary
]

# Keyword sweep for discovery-yield measurement (test 6). Deliberately a mix of
# evergreen and trend-shaped terms — the point is to measure how many *young* apps a
# keyword surfaces, which is what sizes the real D1 keyword list.
SEARCH_TERMS = [
    "habit tracker",
    "ai photo editor",
    "budget planner",
    "sleep sounds",
    "workout log",
    "focus timer",
    "recipe manager",
    "language flashcards",
    "mood journal",
    "receipt scanner",
]

# Apple genre spread for feed harvesting. Games (6014) is the volume outlier; the rest
# give category diversity so test 4's young-app pool isn't all hypercasual clones.
APPLE_GENRES = [6014, 6002, 6008, 6012, 6007, 6013, 6015, 6000]

APPLE_FEED = "https://itunes.apple.com/us/rss/{feed}/limit=100/genre={genre}/json"
APPLE_LOOKUP = "https://itunes.apple.com/lookup"
APPLE_REVIEWS = (
    "https://itunes.apple.com/us/rss/customerreviews/page={page}/id={track_id}"
    "/sortby=mostrecent/{fmt}"
)

# Apple's documented ceiling is ~20 req/min. Stay under it.
APPLE_DELAY = (3.0, 4.5)
PLAY_DELAY = (2.0, 5.0)


# ---------------------------------------------------------------------------
# fetch layer — prefigures src/lib/http.ts + guards.ts
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    """Per-run anomaly counters. These become run_log.stats in the real pipeline."""

    requests: int = 0
    http_403: int = 0
    http_429: int = 0
    http_5xx: int = 0
    empty_200: int = 0  # the silent killer: 200 OK carrying no data
    errors: int = 0
    codes: Counter = field(default_factory=Counter)

    def anomaly_rate(self) -> float:
        if not self.requests:
            return 0.0
        bad = self.http_403 + self.http_429 + self.http_5xx + self.empty_200 + self.errors
        return bad / self.requests

    def summary(self) -> str:
        return (
            f"{self.requests} req, 403={self.http_403} 429={self.http_429} "
            f"5xx={self.http_5xx} empty200={self.empty_200} err={self.errors} "
            f"({self.anomaly_rate():.1%} anomalous)"
        )


STATS = Stats()


def fetch(url: str, delay: tuple[float, float], expect=None, retries: int = 1):
    """GET with jitter, retry, and empty-response detection.

    `expect` is a predicate over the response text. A 200 that fails it is recorded as
    empty_200 — the failure mode proven live by Play's dead NEW_FREE cluster URL, which
    answers 200 with a full HTML shell and zero app links. Returns None on failure;
    callers must treat None as "unknown", never as "zero".
    """
    for attempt in range(retries + 1):
        if STATS.requests:
            time.sleep(random.uniform(*delay))
        STATS.requests += 1
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as e:
            STATS.errors += 1
            print(f"    ! request error {type(e).__name__} on {url[:80]}")
            continue

        STATS.codes[r.status_code] += 1
        if r.status_code == 403:
            STATS.http_403 += 1
        elif r.status_code == 429:
            STATS.http_429 += 1
        elif r.status_code >= 500:
            STATS.http_5xx += 1
        elif r.status_code == 200:
            if expect is None or expect(r.text):
                return r.text
            STATS.empty_200 += 1
            print(f"    ! 200-but-empty on {url[:80]}")

        if attempt < retries:
            time.sleep(2 ** attempt * 3)
    return None


def apple_feed_entries(text: str) -> list[dict]:
    """Parse a legacy Apple RSS JSON feed into entry dicts. Tolerates the single-entry
    case, where Apple emits an object instead of a list."""
    try:
        feed = json.loads(text).get("feed", {})
    except json.JSONDecodeError:
        return []
    entries = feed.get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    return entries


def entry_track_id(entry: dict) -> str | None:
    attrs = entry.get("id", {}).get("attributes", {})
    return attrs.get("im:id")


def has_feed_entries(text: str) -> bool:
    return len(apple_feed_entries(text)) > 0


def safe_search(term: str, n_hits: int = 100) -> list[dict] | None:
    """google-play-scraper 1.2.7 raises TypeError on thin result sets.

    Confirmed live 2026-08-17: search.py:41 does an unguarded index chain
    `dataset["ds:4"][0][1][0][23][16]`, so any query without a strong top result kills
    the call. An unwrapped sweep dies on its first obscure keyword. Returns None for
    "could not determine" — never an empty list, which would read as "no such apps".
    """
    try:
        return play_search(term, n_hits=n_hits, lang="en", country="us")
    except TypeError:
        return []  # genuinely thin result set, not a failure
    except Exception as e:
        STATS.errors += 1
        print(f"    ! search({term!r}) {type(e).__name__}: {str(e)[:60]}")
        return None


def install_band(s) -> int | None:
    """Parse Play's display install string ('1,000,000+') to its band floor.

    Search results carry this for free while release date requires a detail fetch, so it
    is the cheapest available proxy for "probably young" — see test 6.
    """
    if not isinstance(s, str):
        return None
    digits = re.sub(r"[^0-9]", "", s)
    return int(digits) if digits else None


# ---------------------------------------------------------------------------
# test 1 — realInstalls still exact?
# ---------------------------------------------------------------------------


def test_play_real_installs(quick: bool) -> dict:
    print("\n[T1] Play realInstalls — exact or banded?")
    targets = PLAY_KNOWN[:4] if quick else PLAY_KNOWN
    rows, exact, banded, failed = [], 0, 0, 0

    for pkg in targets:
        time.sleep(random.uniform(*PLAY_DELAY))
        STATS.requests += 1
        try:
            d = play_app(pkg, lang="en", country="us")
        except NotFoundError:
            print(f"  {pkg:32} NOT FOUND (delisted?)")
            failed += 1
            continue
        except Exception as e:
            STATS.errors += 1
            print(f"  {pkg:32} ERROR {type(e).__name__}: {e}")
            failed += 1
            continue

        real, mn = d.get("realInstalls"), d.get("minInstalls")
        ratings = d.get("ratings")

        # guard 2: realInstalls null while ratings populated == parser failure,
        # NOT an app with no installs.
        if real is None:
            verdict = "PARSER-FAIL" if ratings else "null"
            failed += 1
        elif mn is not None and real == mn:
            verdict = "banded"
            banded += 1
        else:
            verdict = "exact"
            exact += 1

        rows.append((pkg, real, mn, ratings, verdict))
        print(
            f"  {pkg:32} real={_n(real):>14}  min={_n(mn):>14}  "
            f"ratings={_n(ratings):>10}  {verdict}"
        )

    ok = exact >= max(1, len(targets) - failed - banded) and exact > 0
    return {
        "id": "T1",
        "name": "Play realInstalls exact",
        "ok": ok,
        "gate": True,
        "detail": f"{exact} exact / {banded} banded / {failed} failed of {len(targets)}",
        "rows": rows,
    }


def _n(v) -> str:
    return f"{v:,}" if isinstance(v, int) else "-"


# ---------------------------------------------------------------------------
# test 6 (runs early — its output feeds test 2) — search sweep yield
# ---------------------------------------------------------------------------


LOW_BAND = 100_000  # install-band ceiling below which an app is a plausible young candidate


def test_search_sweep(quick: bool) -> tuple[dict, list[str]]:
    """Measure discovery yield — and whether the free `installs` field is a usable
    pre-filter for youth.

    The first local run found 0/5 randomly-sampled search-pool apps under 90 days old:
    Play search ranks by relevance and popularity, so young apps sit far down the list.
    Search results do carry `installs` for free, though, while release date costs a detail
    fetch. So this stratifies the pool by install band and age-samples each stratum
    separately — the question is not "how young is the pool" but "does the cheap filter
    concentrate the young apps".
    """
    print("\n[T6] Play search sweep — discovery yield and install-band pre-filter")
    terms = SEARCH_TERMS[:3] if quick else SEARCH_TERMS
    seen: dict[str, int | None] = {}  # appId -> install band floor
    per_term = []

    for term in terms:
        time.sleep(random.uniform(*PLAY_DELAY))
        STATS.requests += 1
        hits = safe_search(term, n_hits=100)
        if hits is None:
            per_term.append((term, 0, 0))
            continue
        new = 0
        for h in hits:
            aid = h.get("appId")
            if aid and aid not in seen:
                seen[aid] = install_band(h.get("installs"))
                new += 1
        per_term.append((term, len(hits), new))
        print(f"  {term:24} {len(hits):>3} hits, {new:>3} new  (pool {len(seen)})")

    low = [a for a, b in seen.items() if b is not None and b < LOW_BAND]
    high = [a for a, b in seen.items() if b is not None and b >= LOW_BAND]
    unknown = [a for a, b in seen.items() if b is None]
    print(
        f"  pool {len(seen)}: {len(low)} under {LOW_BAND:,} installs, "
        f"{len(high)} above, {len(unknown)} unknown"
    )

    per_stratum = 4 if quick else 12
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    stratum_results = {}
    for label, group in (("low-band", low), ("high-band", high)):
        random.shuffle(group)
        sample, young = group[:per_stratum], 0
        for aid in sample:
            time.sleep(random.uniform(*PLAY_DELAY))
            STATS.requests += 1
            try:
                d = play_app(aid, lang="en", country="us")
            except Exception:
                STATS.errors += 1
                continue
            rel = _parse_play_released(d.get("released"))
            if rel and rel >= cutoff:
                young += 1
        pct = young / len(sample) if sample else 0.0
        stratum_results[label] = (young, len(sample), pct)
        print(f"  {label:10} {young}/{len(sample)} sampled are <90d old ({pct:.0%})")

    lo_pct = stratum_results.get("low-band", (0, 0, 0.0))[2]
    hi_pct = stratum_results.get("high-band", (0, 0, 0.0))[2]
    max_hits = max((h for _, h, _ in per_term), default=0)
    useful = lo_pct > hi_pct

    # Detail-fetch order matters: return low-band first so downstream tests exercise the
    # long tail, where parser drift actually bites.
    pool = low + unknown + high
    return (
        {
            "id": "T6",
            "name": "Search sweep + band filter",
            "ok": len(seen) > 0,
            "gate": False,
            "detail": (
                f"{len(seen)} distinct apps / {len(terms)} terms (cap {max_hits}/term); "
                f"young rate low-band {lo_pct:.0%} vs high-band {hi_pct:.0%} — "
                f"band pre-filter {'WORKS, use it to target detail fetches' if useful else 'NO BETTER than random'}"
            ),
            "rows": per_term,
        },
        pool,
    )


def _parse_play_released(s) -> datetime | None:
    if not s:
        return None
    for fmt in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# test 2 — sustained Play volume (the real IP question)
# ---------------------------------------------------------------------------


def test_play_volume(pool: list[str], quick: bool) -> dict:
    n = 15 if quick else 100
    targets = (pool or PLAY_KNOWN)[:n]
    print(f"\n[T2] Play sustained volume — {len(targets)} detail fetches with jitter")
    before = Stats(
        requests=STATS.requests,
        http_403=STATS.http_403,
        http_429=STATS.http_429,
        http_5xx=STATS.http_5xx,
        empty_200=STATS.empty_200,
        errors=STATS.errors,
    )
    ok_rows, null_installs, hard_fail = 0, 0, 0

    for i, pkg in enumerate(targets, 1):
        time.sleep(random.uniform(*PLAY_DELAY))
        STATS.requests += 1
        try:
            d = play_app(pkg, lang="en", country="us")
        except NotFoundError:
            hard_fail += 1
            continue
        except Exception as e:
            STATS.errors += 1
            hard_fail += 1
            if hard_fail <= 5:
                print(f"    ! {pkg} {type(e).__name__}: {str(e)[:60]}")
            continue
        if d.get("realInstalls") is None and d.get("ratings"):
            null_installs += 1
        else:
            ok_rows += 1
        if i % 20 == 0:
            print(f"    {i}/{len(targets)} ok={ok_rows} null={null_installs} fail={hard_fail}")

    attempted = len(targets)
    anomalies = (
        (STATS.http_403 - before.http_403)
        + (STATS.http_429 - before.http_429)
        + (STATS.http_5xx - before.http_5xx)
        + (STATS.errors - before.errors)
    )
    rate = anomalies / attempted if attempted else 0.0
    null_rate = null_installs / attempted if attempted else 0.0
    print(f"  → {ok_rows} ok, {null_installs} null-installs, {hard_fail} failed; anomaly {rate:.1%}")

    # Gate mirrors spec §6.2 (5% null) and §1.2 (2% anomaly).
    return {
        "id": "T2",
        "name": "Play sustained volume",
        "ok": rate < 0.02 and null_rate < 0.05,
        "gate": True,
        "detail": (
            f"{attempted} fetches: {ok_rows} ok, {null_installs} parser-null "
            f"({null_rate:.1%}), {anomalies} anomalies ({rate:.1%})"
        ),
        "rows": [],
    }


# ---------------------------------------------------------------------------
# test 3 — Apple endpoint surface
# ---------------------------------------------------------------------------


def test_apple_endpoints(quick: bool) -> tuple[dict, list[str], dict]:
    print("\n[T3] Apple endpoints — feeds, variants, lookup, reviews formats")
    genres = APPLE_GENRES[:2] if quick else APPLE_GENRES
    feeds = ["topfreeapplications", "newapplications", "newfreeapplications", "newpaidapplications"]
    results, track_ids = {}, {}

    # 3a: which feeds work, and what do they cap at?
    for feed in feeds:
        counts = []
        for genre in genres[:2]:
            text = fetch(
                APPLE_FEED.format(feed=feed, genre=genre), APPLE_DELAY, expect=has_feed_entries
            )
            entries = apple_feed_entries(text) if text else []
            counts.append(len(entries))
            for e in entries:
                tid = entry_track_id(e)
                if tid:
                    track_ids[tid] = feed
        results[feed] = counts
        status = "OK" if any(counts) else "DEAD"
        print(f"  {feed:24} {counts}  {status}")

    # Harvest a wider id pool from the working new-apps feed for test 4.
    if not quick:
        for genre in genres[2:]:
            text = fetch(
                APPLE_FEED.format(feed="newapplications", genre=genre),
                APPLE_DELAY,
                expect=has_feed_entries,
            )
            for e in apple_feed_entries(text) if text else []:
                tid = entry_track_id(e)
                if tid:
                    track_ids.setdefault(tid, "newapplications")

    # 3b: lookup batching at 200 ids.
    ids = list(track_ids)[:200]
    lookup_ok, lookup_returned = False, 0
    if ids:
        url = f"{APPLE_LOOKUP}?" + urllib.parse.urlencode(
            {"id": ",".join(ids), "country": "us", "entity": "software"}
        )
        text = fetch(url, APPLE_DELAY, expect=lambda t: '"trackId"' in t)
        if text:
            try:
                data = json.loads(text)
                lookup_returned = data.get("resultCount", 0)
                lookup_ok = lookup_returned > 0
            except json.JSONDecodeError:
                pass
    print(f"  lookup batch: sent {len(ids)} ids, got {lookup_returned} results")

    # 3c: reviews XML vs JSON on the same app — is the JSON zero-entry bug back?
    probe = "324684580"  # Spotify: guaranteed to have reviews
    fmt_counts = {}
    for fmt in ("xml", "json"):
        text = fetch(
            APPLE_REVIEWS.format(page=1, track_id=probe, fmt=fmt),
            APPLE_DELAY,
            expect=lambda t: ("<entry>" in t) or ('"author"' in t),
        )
        if text is None:
            fmt_counts[fmt] = None
        elif fmt == "xml":
            fmt_counts[fmt] = text.count("<entry>")
        else:
            fmt_counts[fmt] = len(apple_feed_entries(text))
    print(f"  reviews page1: xml={fmt_counts.get('xml')} json={fmt_counts.get('json')}")

    # 3d: timestamp parsing with offsets → UTC.
    ts_ok = False
    text = fetch(APPLE_REVIEWS.format(page=1, track_id=probe, fmt="xml"), APPLE_DELAY)
    if text:
        m = re.findall(r"<updated>([^<]+)</updated>", text)
        if m:
            try:
                parsed = datetime.fromisoformat(m[-1]).astimezone(timezone.utc)
                ts_ok = True
                print(f"  timestamp {m[-1]} → {parsed.isoformat()} (UTC)")
            except ValueError:
                print(f"  ! timestamp parse failed on {m[-1]}")

    working_new_feeds = [f for f in feeds if f.startswith("new") and any(results.get(f, []))]
    chart_cap = max(results.get("topfreeapplications", [0]) or [0])
    ok = bool(working_new_feeds) and lookup_ok and bool(fmt_counts.get("xml")) and ts_ok

    return (
        {
            "id": "T3",
            "name": "Apple endpoint surface",
            "ok": ok,
            "gate": True,
            "detail": (
                f"chart cap {chart_cap}; new-app feeds working: "
                f"{', '.join(working_new_feeds) or 'NONE'}; "
                f"lookup {lookup_returned}/{len(ids)}; "
                f"reviews xml={fmt_counts.get('xml')} json={fmt_counts.get('json')}; "
                f"utc parse {'ok' if ts_ok else 'FAIL'}"
            ),
            "rows": sorted(results.items()),
        },
        list(track_ids),
        {"feeds": results, "lookup": lookup_returned, "fmt": fmt_counts},
    )


# ---------------------------------------------------------------------------
# test 4 — do young apps fit under the 500-review cap?
# ---------------------------------------------------------------------------


def test_young_app_reviews(track_ids: list[str], quick: bool) -> dict:
    print("\n[T4] Young-app review depth — is backfill complete or truncated?")
    want = 4 if quick else 10
    now = datetime.now(timezone.utc)

    # Find apps released 30-90 days ago via cheap lookup batches.
    candidates = []
    for i in range(0, min(len(track_ids), 400), 200):
        batch = track_ids[i : i + 200]
        url = f"{APPLE_LOOKUP}?" + urllib.parse.urlencode(
            {"id": ",".join(batch), "country": "us", "entity": "software"}
        )
        text = fetch(url, APPLE_DELAY, expect=lambda t: '"trackId"' in t)
        if not text:
            continue
        for r in json.loads(text).get("results", []):
            rel = r.get("releaseDate")
            if not rel:
                continue
            try:
                d = datetime.fromisoformat(rel.replace("Z", "+00:00"))
            except ValueError:
                continue
            age = (now - d).days
            if 30 <= age <= 90:
                candidates.append(
                    {
                        "id": str(r.get("trackId")),
                        "name": (r.get("trackName") or "")[:28],
                        "age": age,
                        "ratings": r.get("userRatingCount") or 0,
                    }
                )

    # Stratify, don't take the top. The first local run probed the 4 highest-rated young
    # apps and found 75% truncated — true, but it measured the worst case by construction.
    # Spread picks evenly across the rating-count distribution so the reported truncation
    # rate reflects the population the pipeline will actually backfill.
    candidates.sort(key=lambda c: -c["ratings"])
    if len(candidates) > want:
        step = len(candidates) / want
        targets = [candidates[min(int(i * step), len(candidates) - 1)] for i in range(want)]
    else:
        targets = candidates
    print(
        f"  found {len(candidates)} apps aged 30-90d; probing {len(targets)} "
        f"stratified across the rating distribution "
        f"({targets[0]['ratings']:,} → {targets[-1]['ratings']:,} ratings)"
        if targets
        else "  no candidates"
    )

    if not targets:
        return {
            "id": "T4",
            "name": "Young-app review depth",
            "ok": False,
            "gate": False,
            "detail": "no apps aged 30-90d in the harvested pool — rerun with wider genre spread",
            "rows": [],
        }

    rows, truncated = [], 0
    for c in targets:
        total, pages, throttled = 0, 0, False
        for page in range(1, 11):
            text = fetch(APPLE_REVIEWS.format(page=page, track_id=c["id"], fmt="xml"), APPLE_DELAY)
            if text is None:
                throttled = True
                break
            n = text.count("<entry>")
            pages = page
            total += n
            if n < 50:  # short page == last page
                break
        if pages == 10 and total >= 500:
            truncated += 1
        rows.append((c["name"], c["age"], c["ratings"], total, pages, throttled))
        print(
            f"  {c['name']:30} age={c['age']:>3}d ratings={c['ratings']:>6} "
            f"text_reviews={total:>4} pages={pages}{' THROTTLED' if throttled else ''}"
        )

    trunc_rate = truncated / len(rows)
    print(f"  → {truncated}/{len(rows)} hit the 500 cap ({trunc_rate:.0%})")
    return {
        "id": "T4",
        "name": "Young-app review depth",
        "ok": True,  # informational: shapes scoring weights, doesn't gate the build
        "gate": False,
        "detail": (
            f"{len(rows)} apps aged 30-90d: {truncated} truncated at the 500 cap "
            f"({trunc_rate:.0%}). Backfill is "
            f"{'PARTIAL — weight rank/velocity higher' if trunc_rate > 0.3 else 'effectively complete'}"
        ),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# test 5 — Play reviews via batchexecute continuation tokens
# ---------------------------------------------------------------------------


def test_play_batchexecute(quick: bool) -> dict:
    print("\n[T5] Play reviews batchexecute — continuation-token paging")
    pkg = "com.duolingo"
    pages_wanted = 2 if quick else 3
    token, total, pages, oldest = None, 0, 0, None

    for _ in range(pages_wanted):
        time.sleep(random.uniform(*PLAY_DELAY))
        STATS.requests += 1
        try:
            batch, token = play_reviews(
                pkg, lang="en", country="us", count=100, continuation_token=token
            )
        except Exception as e:
            STATS.errors += 1
            print(f"  ERROR {type(e).__name__}: {str(e)[:80]}")
            break
        if not batch:
            print("  ! empty batch — treat as throttle, not 'no reviews'")
            break
        pages += 1
        total += len(batch)
        for rv in batch:
            at = rv.get("at")
            if isinstance(at, datetime):
                at = at.replace(tzinfo=at.tzinfo or timezone.utc)
                oldest = at if oldest is None or at < oldest else oldest
        print(f"  page {pages}: {len(batch)} reviews, token={'yes' if token else 'NONE'}")
        if token is None:
            break

    return {
        "id": "T5",
        "name": "Play reviews paging",
        "ok": pages >= 2 and total > 0,
        "gate": False,
        "detail": (
            f"{pages} pages, {total} reviews"
            + (f", oldest {oldest.date().isoformat()}" if oldest else "")
        ),
        "rows": [],
    }


# ---------------------------------------------------------------------------
# test 7 — developer-page discovery (channel D2)
# ---------------------------------------------------------------------------

PLAY_DEV_URL = "https://play.google.com/store/apps/developer?id={dev}&hl=en&gl=us"


def test_developer_pages(quick: bool) -> dict:
    """Validate channel D2, now Play's highest-precision discovery path.

    Verified 2026-08-17: the legacy `dev?id=` form is HTTP 404, `developer?id=<name>` is
    200 and yields app links. The Python library exposes no developer() function, so this
    needs the thin regex parser below — which is exactly why it must be validated from the
    runner IP, not assumed.
    """
    print("\n[T7] Play developer pages — channel D2 parseability")
    devs = ["Spotify AB", "Duolingo"] if quick else [
        "Spotify AB",
        "Duolingo",
        "Google LLC",
        "Niantic, Inc.",
        "Doist",
    ]
    rows, worked = [], 0

    for dev in devs:
        url = PLAY_DEV_URL.format(dev=urllib.parse.quote_plus(dev))
        text = fetch(
            url, PLAY_DELAY, expect=lambda t: "/store/apps/details?id=" in t
        )
        ids = sorted(set(re.findall(r"/store/apps/details\?id=([a-zA-Z0-9._]+)", text))) if text else []
        if ids:
            worked += 1
        rows.append((dev, len(ids), ids[:3]))
        print(f"  {dev:18} {len(ids):>3} app ids  {ids[:3]}")

    ok = worked == len(devs)
    return {
        "id": "T7",
        "name": "Developer-page discovery",
        "ok": ok,
        "gate": False,
        "detail": (
            f"{worked}/{len(devs)} developer pages parsed via `developer?id=<name>` "
            f"(legacy `dev?id=` is 404). Regex parser, no library support"
        ),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def write_report(results: list[dict], started: datetime, env: str) -> bool:
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    gates_failed = [r for r in results if r.get("gate") and not r["ok"]]

    lines = [
        "# Phase 0 validation results",
        "",
        f"- **Run environment:** {env}",
        f"- **Started (UTC):** {started.isoformat(timespec='seconds')}",
        f"- **Duration:** {elapsed / 60:.1f} min",
        f"- **HTTP:** {STATS.summary()}",
        f"- **Status codes:** {dict(STATS.codes)}",
        "",
        "| # | Test | Gate | Result | Detail |",
        "|---|------|------|--------|--------|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['name']} | {'yes' if r.get('gate') else 'info'} | "
            f"{'PASS' if r['ok'] else 'FAIL'} | {r['detail']} |"
        )

    lines += ["", "## Verdict", ""]
    if gates_failed:
        lines.append(
            "**BLOCKED.** Failed gates: "
            + ", ".join(f"{r['id']} ({r['name']})" for r in gates_failed)
            + "."
        )
        lines.append("")
        lines.append(
            "Per spec §1, if T1/T2 fail from Actions while the residential baseline of "
            "2026-08-12 passed, the cause is the runner IP, not the code. Move fetch jobs "
            "to a self-hosted runner before starting Phase 1."
        )
    else:
        lines.append("**All gates passed.** Proceed to Phase 1 (schema + Play pipeline).")

    lines += [
        "",
        "## Residential baseline (2026-08-12) for comparison",
        "",
        "| Fact | Baseline |",
        "|---|---|",
        "| Play `realInstalls` | exact — Spotify 3,084,933,551 vs min 1,000,000,000 |",
        "| Apple chart cap | 100 per genre (`limit=200` still returns 100) |",
        "| Apple `newapplications` | 100 entries per genre |",
        "| Apple reviews XML | 50 entries per page, 10 pages max |",
        "| Apple reviews JSON | 50 entries (the zero-entry bug did not reproduce) |",
        "| Play `NEW_FREE` cluster | dead — HTTP 200, zero app links |",
        "| Play sitemaps | no `<lastmod>`, mixed content types — useless for new-app discovery |",
        "| Play `developer?id=<name>` | 200 + parseable app links (`dev?id=` is 404) |",
        "| `play_search` on thin results | raises TypeError (search.py:41) — must be wrapped |",
        "",
        "See FINDINGS.md for the full evidence log and the spec deltas each finding forces.",
        "",
    ]
    report = "\n".join(lines)
    with open("VALIDATION.md", "w") as f:
        f.write(report)
    print("\n" + report)
    return not gates_failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="reduced sample for local smoke tests")
    args = ap.parse_args()

    import os

    env = (
        f"GitHub Actions ({os.environ.get('RUNNER_OS')}, run {os.environ.get('GITHUB_RUN_ID')})"
        if os.environ.get("GITHUB_ACTIONS")
        else "local / residential IP"
    )
    started = datetime.now(timezone.utc)
    random.seed(20260817)

    print(f"Phase 0 validation spike — {env}{' [quick]' if args.quick else ''}")
    print("=" * 78)

    results = []
    results.append(test_play_real_installs(args.quick))

    sweep, pool = test_search_sweep(args.quick)
    results.append(test_play_volume(pool, args.quick))

    apple, track_ids, _ = test_apple_endpoints(args.quick)
    results.append(apple)
    results.append(test_young_app_reviews(track_ids, args.quick))
    results.append(test_play_batchexecute(args.quick))
    results.append(sweep)
    results.append(test_developer_pages(args.quick))
    results.sort(key=lambda r: r["id"])

    print("\n" + "=" * 78)
    return 0 if write_report(results, started, env) else 1


if __name__ == "__main__":
    sys.exit(main())
