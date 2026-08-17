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
| 1 | schema + Play pipeline | **code complete**, unrun against a live database (needs `DATABASE_URL`) |
| 2 | iOS pipeline + review backfill | not started |
| 3 | scoring + run_log | not started |
| 4 | dashboard | not started |
| 5 | trust heuristics, twin matching, retention | not started |

## The asymmetry worth knowing before reading any output

The two stores are not mirror images, and Phase 0 proved it (FINDINGS.md §3):

- **iOS — day-zero discovery, coarser velocity.** Three working new-app RSS feeds, ~100
  per genre. Apps are catchable the day they ship. Velocity comes from `Δ userRatingCount`.
- **Play — lagging discovery, exact velocity.** `NEW_FREE` is dead, sitemaps are dateless,
  and search ranks by popularity, so apps are usually found *after* some traction. But once
  found, `Δ realInstalls` is the truest growth signal in either store.

So an iOS row can be a genuine day-3 catch; a Play row usually means "already moving, now
measured precisely." Play will systematically miss the earliest window — that surface is
simply not exposed any more.

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
