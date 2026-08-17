# Breakout Detector

Finds newly released apps that are accelerating, across Google Play and the App Store.
Daily GitHub Actions cron → Postgres (Supabase) → dashboard.

- **[SPEC.md](SPEC.md)** — the implementation spec, built in phase order
- **[FINDINGS.md](FINDINGS.md)** — what was measured live, and the spec changes each finding forced
- **[VALIDATION.md](VALIDATION.md)** — latest Phase 0 result table

## Status

| Phase | Ships | State |
|---|---|---|
| 0 | validation spike | **done** — Actions runner passes all gates, 193 requests at 0% anomalies ([FINDINGS.md §0](FINDINGS.md)) |
| 1 | schema + Play pipeline | **done** — running daily on Actions against Supabase |
| 2 | iOS pipeline + review backfill | **done** — feeds, batched lookup, review backfill all run live |
| 3 | scoring + run_log | **done** — 645 apps scored, every input persisted in `components` |
| 4 | dashboard | **built and verified** against live data; needs a Vercel account to deploy |
| 5 | trust heuristics, twin matching, retention | **done** — 79 twin pairs, weekly rollup ready |

## The asymmetry worth knowing before reading any output

The two stores are not mirror images, and — importantly — **neither store's obvious channel
is the one that works.** Both had to be found by measuring release dates in real data;
Phase 0 got both of them backwards (FINDINGS.md §3 and §4b).

| | Play | iOS |
|---|---|---|
| Velocity signal | `Δ realInstalls` — **exact daily installs** | `Δ userRatingCount` — ~20× denser than text reviews |
| Channel that actually finds new apps | **keyword search** (53% under 120d, youngest 1 day) | **charts** (youngest 0 days, often at high rank) |
| Channel that sounds right but isn't | charts — median find is 2,005 days old | `newapplications` — frozen since 2026-07-07 |
| Cheapest youth filter | install band under 100 → 93% are under 120 days | none; chart rank is the proxy |
| Enrichment cost | 1 request per app | 200 apps per request |

So an iOS row can be a genuine day-0 catch, while a Play row usually means "already moving,
now measured precisely." Play's earliest window is genuinely lost — that surface is not
exposed any more — but its numbers, once found, are the truest in either store.

**Velocity is never combined across stores.** Installs and ratings are different units;
scores are z-ranked within a store so an app only competes with apps measured the same way.

## Setup

Python 3.12. No Node needed for the pipeline (the dashboard is Phase 4 and builds on Vercel).

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `DATABASE_URL` (Supabase → Project Settings →
Database → Connection string → URI; prefer the pooler on 6543 for short Actions jobs).

## Running

```bash
./.venv/bin/python -m src.jobs.migrate
```

```bash
./.venv/bin/python -m src.jobs.discover --channels chart,search,developer
```

```bash
./.venv/bin/python -m src.jobs.enrich --limit 400
```

Then the Phase 1 acceptance query:

```bash
psql "$DATABASE_URL" -c 'select name, developer, days_since_release, installs_per_day from new_play_apps_by_install_rate limit 20'
```

Tests (no network, no database):

```bash
./.venv/bin/python -m unittest discover -s tests -t .
```

The Phase 0 spike, which does hit the network:

```bash
./.venv/bin/python spike/validate.py --quick
```

## Rules this codebase actually enforces

1. **Every network call goes through `src/lib/http.py`.** Including google-play-scraper's,
   via `Fetcher.guarded()`. Throttling, backoff, and IP-degradation detection live there;
   a stray `requests.get` defeats all three.
2. **A job that cannot distinguish "no data" from "blocked" fails loudly.** Failed fetches
   return `None`, never `0`/`[]`/`""`. A zero written today is indistinguishable from a
   measurement next week, and it corrupts every delta computed across it.
3. **Status codes lie.** Play's dead `NEW_FREE` cluster answers HTTP 200 with a full HTML
   shell and zero app links. Every fetch asserts on content, not status.
4. **`snapshot.day` is stamped from intent, not the clock.** Actions cron fires hours late;
   `--day` makes a replayed run land on the day it actually covers.
5. **Parser drift is a rate, not an event.** One null is attrition; 5% of a batch means
   Google changed the page and the whole run is suspect.
