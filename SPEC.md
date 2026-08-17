# Breakout Detector — Implementation Spec v2

Hand this to a coding agent. Written to be built in order. Supersedes v1: the two claims the
architecture pivoted on were **live-verified on 2026-08-12 from a residential IP**, so Phase 0
shrinks to the one question that remains — what a GitHub Actions runner IP can get away with.

---

## 0. Verification status

| Claim | Status | Consequence |
|---|---|---|
| Play `realInstalls` returns exact installs | **VERIFIED LIVE 2026-08-12** — Spotify `3,084,933,551` vs `minInstalls 1,000,000,000`; Duolingo `952,831,136`; Telegram `2,944,237,356` | Architecture stands. Play is the exact-velocity store |
| Play detail page server-rendered: metadata + histogram + `released` + `ratings` in one request | Verified via pinned Python `google-play-scraper` | Detail scraping cheap and stable |
| Written reviews ≈ 5% of star ratings | Re-verified: Spotify 36,104,389 ratings / 1,847,752 reviews = 5.1% | Calibration constant `RATINGS_PER_REVIEW ≈ 20`. Never equate reviews with ratings |
| Apple legacy charts RSS alive, cap **100** | Verified — `limit=200` returns exactly 100 entries | Chart diffing works; 100/genre is the ceiling |
| Apple `newapplications` RSS feed alive | **VERIFIED — 100 entries/genre.** v1 didn't use this | Direct iOS new-app discovery, pre-chart. Use it as the primary iOS channel |
| Apple reviews RSS XML, 10 pages × 50 | Verified — 50 entries/page, timestamps carry local offsets (`-07:00`) | Backfill works. **Normalize all timestamps to UTC** |
| Apple reviews JSON returns zero (bug) | **Not reproducible today** — JSON returned 50 reviews + 1 app-header entry | Bug is intermittent at worst. Still use XML; JSON's first entry is the app itself, one more parsing trap |
| iTunes Lookup batches 200 ids | Verified (smoke test) | Cheap enrichment |
| Play `NEW_FREE` / `NEW_PAID` collections | **DEAD** — cluster URL answers HTTP 200, full HTML shell, **zero** app links | Discovery must not depend on it. Also the canonical guard test case: status codes lie |
| Play publishes sitemaps | Verified — robots.txt lists indexes; **82,937** gzipped shards per index snapshot | Full-catalog enumeration exists in principle; far beyond an Actions job. Parked (§5) |
| GitHub Actions runner IP tolerated by either store | **UNVERIFIED — now the #1 unknown** | Everything above was residential. Phase 0 runs the same tests from Actions |
| <90-day apps have <500 reviews (full backfill) | Unverified | Phase 0 test 4 |
| Play reviews `batchexecute` works from Actions | Docs-confirmed only | Phase 0 test 5 |

---

## 1. Phase 0 — validation from Actions (~1 hour of code, one workflow run)

One throwaway script, `spike/validate.ts`, run **as a GitHub Actions workflow** — the residential
results are already known, so the runner IP is the only thing being tested. It prints a pass/fail
table and commits it to the repo as `VALIDATION.md`. **Do not start Phase 1 until this runs.**

1. **Play detail × 10 apps** of varying size. Assert `realInstalls` present and ≠ `minInstalls`.
2. **Sustained volume:** Play detail × 100 apps, 2–5 s jitter. Count HTTP 403/429 and
   200-but-empty responses. Pass = anomalies < 2%.
3. **Apple endpoints:** 2 chart genres; `newapplications` **plus the `newfreeapplications` /
   `newpaidapplications` variants** (untested); reviews XML all 10 pages × 2 apps; one 200-id
   lookup batch.
4. **Young-app review distribution:** 10 apps released 30–90 days ago — how many exceed 500
   reviews? Decides how much weight backfilled history can carry.
5. **Play reviews `batchexecute`:** one app, 3 continuation pages.
6. **Search-sweep yield:** 10 keywords → count distinct app IDs, and how many are <90 days old.
   Decides the keyword-list size discovery needs.

**Decision gate:** if tests 1–2 fail from Actions, move fetch jobs to a self-hosted runner on the
home Mac (launchd-managed) — residential IP, known-good as of 2026-08-12. Local footnote: `node`
is not on the default non-interactive PATH on that machine; CI installs Node 22 regardless.

---

## 2. Architecture

```
GitHub Actions (cron)
  ├── daily.yml:    discover-play + discover-ios → enrich → score   (sequential)
  └── backfill.yml: review history, own schedule, time-boxed        (independent)
                ↓
        Supabase (Postgres + RLS)
                ↓
    Next.js dashboard on Vercel (single user)
```

Two independent store pipelines. **Do not gate either on cross-store matching** — matching is a
later enrichment, not a dependency.

Scoring runs as a **SQL function invoked by the Actions workflow**, not pg_cron and not an Edge
Function: it must run *after* enrichment, and Actions already owns that ordering.

**Backfill is decoupled from daily** (v1 ran it inline). Reviews are the slow, rate-limited part;
inside daily's 50-minute budget they starve scoring. Backfill gets its own schedule and time-box
(§8). `reviews_backfilled = false` is already the queue.

Two operational landmines this design must survive:

1. **GitHub auto-disables scheduled workflows after 60 days without repository activity.** A
   scraper that only writes to Supabase generates none. The last step of `daily.yml` re-enables
   itself via the API (§8) — no commits needed.
2. **Supabase free tier pauses projects after ~a week without API activity.** The daily job keeps
   it warm — but if Actions dies, both cascade. The failure email (§6) is the tripwire.

---

## 3. Repo structure

```
/
├── .github/workflows/
│   ├── daily.yml              # discovery → enrich → score, sequential
│   └── backfill.yml           # scheduled + manual dispatch, time-boxed
├── spike/
│   └── validate.ts            # Phase 0. Output committed as VALIDATION.md, then delete the script
├── src/
│   ├── stores/
│   │   ├── ios/
│   │   │   ├── feeds.ts       # charts + newapplications RSS (same shape)
│   │   │   ├── lookup.ts      # iTunes Lookup, 200-id batching
│   │   │   └── reviews.ts     # customer reviews RSS (XML)
│   │   └── play/
│   │       ├── discover.ts    # search sweep + developer pages + similar + charts
│   │       ├── detail.ts      # pinned python lib via subprocess; thin-parser fallback
│   │       └── reviews.ts     # batchexecute + continuation tokens
│   ├── lib/
│   │   ├── http.ts            # ONE fetch wrapper. Throttle, retry, jitter, UA
│   │   ├── guards.ts          # empty-response + negative-delta detection. See §6
│   │   ├── db.ts              # Supabase client
│   │   └── log.ts             # structured run logging → run_log
│   ├── jobs/
│   │   ├── discover.ts
│   │   ├── enrich.ts
│   │   ├── backfill.ts
│   │   └── score.ts
│   └── types.ts
├── sql/
│   ├── 001_schema.sql
│   ├── 002_rls.sql
│   ├── 003_scoring.sql        # score as a SQL function
│   └── 004_retention.sql
└── dashboard/                 # Next.js App Router
    ├── app/page.tsx           # sortable table + filters
    ├── app/app/[id]/page.tsx  # detail: velocity sparkline (installs on Play, ratings on iOS)
    └── lib/supabase.ts
```

**Every network call goes through `lib/http.ts`.** No exceptions. Throttling, backoff, and
IP-failure detection live there; scattered fetches defeat all three.

**Play detail tooling:** invoke the Python `google-play-scraper` as a subprocess emitting JSON on
stdout, **version-pinned** in `requirements.txt` (verified working under Python 3.12). Fallback: a
thin parser for the ~8 fields needed from the server-rendered page. Do not build on the
maintenance-mode JS parser.

---

## 4. Schema

```sql
create type store_kind as enum ('ios','play');

create table app (
  id                  bigserial primary key,
  store               store_kind not null,
  store_app_id        text not null,
  name                text,
  developer           text,
  developer_id        text,
  category            text,
  released            date,
  price               numeric default 0,
  icon_url            text,
  first_seen          timestamptz not null default now(),
  discovered_via      text,                 -- 'newapps_feed' | 'chart' | 'search' | 'developer' | 'similar'
  last_enriched       timestamptz,
  reviews_backfilled  boolean not null default false,
  delisted            boolean not null default false,   -- 404/removed: stop refetching
  twin_app_id         bigint references app(id),
  twin_confidence     numeric check (twin_confidence between 0 and 1),
  clone_suspect       boolean not null default false,
  relaunch_suspect    boolean not null default false,
  unique (store, store_app_id)
);
create index on app (released desc nulls last);
create index on app (store, category);
create index on app (reviews_backfilled) where reviews_backfilled = false;

-- day is the UTC date the row covers (stamped from intent, not wall clock — see §8)
create table snapshot (
  app_id        bigint not null references app(id) on delete cascade,
  day           date not null,
  install_exact bigint,     -- play realInstalls. null on ios
  install_min   bigint,     -- play minInstalls (banded floor)
  rating_count  int,        -- ios userRatingCount (per-storefront) / play ratings
  avg_rating    numeric,
  review_count  int,
  version       text,
  best_rank     int,        -- best position across tracked genre charts, null if unranked
  primary key (app_id, day)
);
create index on snapshot (day desc);

-- day-one history source (iOS backfill; Play reviews for apps already scoring well)
create table review_event (
  app_id    bigint not null references app(id) on delete cascade,
  review_id text not null,
  posted_at timestamptz not null,           -- normalized to UTC at ingest
  rating    int check (rating between 1 and 5),
  version   text,
  country   text not null default 'us',
  primary key (app_id, review_id)
);
create index on review_event (app_id, posted_at desc);

-- plain view, not materialized: nothing to refresh, no ordering footgun.
-- convert to a matview refreshed inside score() only if it ever shows up in EXPLAIN as a problem.
create view review_daily as
  select app_id, (posted_at at time zone 'utc')::date as day, count(*)::int as n
  from review_event group by 1, 2;

-- retention target (v1 referenced a rollup it never defined)
create table snapshot_weekly (
  app_id            bigint not null references app(id) on delete cascade,
  week_start        date not null,          -- monday, utc
  install_exact_max bigint,
  rating_count_max  int,
  review_count_max  int,
  avg_rating_last   numeric,
  best_rank_min     int,
  primary key (app_id, week_start)
);

create table breakout_score (
  app_id     bigint not null references app(id) on delete cascade,
  scored_on  date not null,
  score      numeric not null,
  components jsonb not null,   -- every raw value, z-value, weight used, cold_start flag
  primary key (app_id, scored_on)
);
create index on breakout_score (scored_on desc, score desc);

-- observability. Do not skip this.
create table run_log (
  id         bigserial primary key,
  job        text not null,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  ok         boolean,
  stats      jsonb   -- {fetched, inserted, empty_responses, http_403, http_429, null_rate}
);
```

**RLS:** enable on every table; no anon policies (deny-by-default). The dashboard reads via the
service role from Next.js server components only. Never expose the service key to the browser.

**Retention:** nightly, fold `snapshot` rows older than 60 days into `snapshot_weekly` and delete
them. `review_event` never grows after backfill — keep it indefinitely.

---

## 5. Endpoint reference

### Apple

**Charts** (legacy RSS, per-genre, cap verified at 100):
```
https://itunes.apple.com/us/rss/topfreeapplications/limit=100/genre={genreId}/json
```

**New apps** (verified live — the primary iOS discovery channel, catches apps pre-chart):
```
https://itunes.apple.com/us/rss/newapplications/limit=100/genre={genreId}/json
```
Same feed shape as charts. Test `newfreeapplications` / `newpaidapplications` variants in Phase 0.
Genres: 6000–6018, 6020–6024, 6026, plus games subgenres 7001–7017. Genre 36 = all apps.

**Lookup** (batch up to 200):
```
https://itunes.apple.com/lookup?id={id1,id2,...}&country=us&entity=software
```
Returns `releaseDate`, `currentVersionReleaseDate`, `averageUserRating`, `userRatingCount`,
`version`, `sellerName`, `genres`, `price`, `trackId`, `bundleId`.
`userRatingCount` is **per-storefront** and is the iOS velocity source (§7): snapshot it daily and
difference it. It is also **dev-resettable on version release** — see guard 6.

**Reviews** (XML, 10 pages × 50, most-recent-first, text reviews only ≈ 5% of ratings):
```
https://itunes.apple.com/us/rss/customerreviews/page={1..10}/id={trackId}/sortby=mostrecent/xml
```
Timestamps in `<updated>` carry local offsets — normalize to UTC. Documented rate limit
~20 calls/min; real behaviour is variable and per-IP.

### Google Play

**Detail** — one request returns `realInstalls` (exact — verified), `minInstalls`, `installs`,
`released`, `ratings`, `reviews`, `histogram`, `developer`, `developerId`, `genreId`. Via the
pinned Python lib (§3).

**Reviews** — `batchexecute`, continuation-token paginated. Expensive; only pull for apps already
scoring well.

**Discovery** — `NEW_FREE`/`NEW_PAID` are dead (verified). Channels, all stamped into
`app.discovered_via`:

| # | Channel | Cost | Notes |
|---|---|---|---|
| D1 | Search sweep over a keyword list | ~30 results/query | Recall-limited; size the list from Phase 0 test 6 |
| D2 | Developer pages of every `developer_id` already in the DB | 1 req/dev | Catches new apps from known devs — high precision |
| D3 | Similar-apps expansion from recent high scorers | bounded, ~50/day | Graph crawl from seeds |
| D4 | Category top-chart diffing | cheap | Late signal — apps that chart have usually already broken out |

**Parked:** Play sitemaps (82,937 gzip shards per index) would give exhaustive catalog diffing but
are far beyond an Actions job. Revisit only if the system earns a bigger box.

**Recall KPI:** discovery is the system's ceiling. Track `median(first_seen − released)` per
channel from day one; review after two weeks and kill channels that only find old apps.

---

## 6. Error handling — the silent failures

These corrupt data rather than crash, which is worse. `lib/guards.ts` must handle each:

1. **Apple reviews throttle** returns HTTP 200 with an empty feed and blank pagination links.
   Empty + blank links = throttled → retry with backoff. **Never write zero reviews as a fact.**
2. **Play parser drift** returns `null` for fields that silently become "no data". `realInstalls`
   null while `ratings` is populated is a *parser failure*, not a zero-install app. Fail the row,
   log it, fail the batch if the null rate exceeds 5%.
3. **Partial pagination.** If page 4 of 10 fails, do not persist pages 1–3 as complete history —
   leave `reviews_backfilled = false` and retry the whole app.
4. **IP degradation is gradual.** Track `empty_responses`, `http_403`, `http_429` in `run_log`
   per run. Threshold: either exceeding 10% of requests fails the run.
5. **200-but-empty pages.** The dead `NEW_FREE` cluster URL answers HTTP 200 with a full HTML
   shell and zero app links — live proof that status codes lie. Every fetcher declares an
   expected-entity assertion; zero entities on a 200 = suspect → one retry → fail the row.
6. **Negative deltas.** `Δ install_exact < 0` is impossible → parser failure, fail the row.
   `Δ rating_count < 0` on iOS = developer reset their ratings on a version release → null that
   window's velocity, set `relaunch_suspect = true` (a reset *is* a relaunch signal), don't score
   the poisoned window.

Rule: **a job that cannot distinguish "no data" from "blocked" must fail loudly, not write zeros.**

**"Alert" means exactly this:** the job calls `process.exit(1)` after logging → the workflow run
goes red → GitHub emails the repo owner (watch the repo). No separate alerting infrastructure.

---

## 7. Scoring

**Eligibility:** `released` within 120 days and at least one velocity source. Older apps are not
scored at all — this keeps the z-score population coherent.

**Velocity `v` (the core signal, per store):**
- **Play:** `Δ install_exact` per day — exact daily installs. The gold signal.
- **iOS:** `Δ rating_count` per day — ~20× denser than text-review arrival, exact, from cheap
  lookup batches.
- **Cold start** (fewer than 2 snapshots, iOS): estimate `v ≈ 20 × review_arrival` from backfilled
  `review_daily` (the 5% calibration). Set `components.cold_start = true`.

**Signal availability by data age** (replaces v1's "sharpens over time" hand-wave):

| Data age | Available |
|---|---|
| Day 1 | `vs_lifetime` (both stores, from cumulative totals ÷ app age); iOS momentum from backfilled review history (×20 calibration); rank if charted |
| Day 2+ | Exact Δ velocities from snapshot pairs |
| Day 14+ | `acceleration` |

**Components:**
```
v7           := avg v over snapshot days (0, 7]
v_prior      := avg v over days (7, min(30, snapshot_history)]     -- DISJOINT window,
                                                                   -- not v1's overlapping r7/r30
momentum     := v7 / max(v_prior, ε)          -- null if history < 10 days → weight shifts to vs_lifetime
vs_lifetime  := v7 / max(lifetime_rate, ε)
lifetime_rate:= cumulative_total / days_since_release
                -- cumulative_total = realInstalls (play) or rating_count (ios), i.e. METADATA.
                -- NEVER count(review_event): iOS backfill is capped at 500, undercounting history.
rank_velocity:= clamp((best_rank_7d_ago − best_rank_today) / 10, −5, +5)
                -- unranked → ranked = +3;  unranked both = 0
volume       := log10(v7 + 1)                 -- kills 1→4/day "4× growth"
recency      := 1 − days_since_release / 240  -- 1.0 at release → 0.5 at the 120-day cutoff.
                                              -- deliberate: no taper-to-zero cliff (v1 zeroed at 90d)
ε            := 5 installs/day (play), 0.5 ratings/day (ios)
```

**Normalization:** winsorize each component at p05/p95, then z-score. Population =
**same store, same `scored_on`**. (Category-relative z is a later refinement — note it in
`components`, don't build it yet.)

```
score := (0.40·z(momentum) + 0.25·z(vs_lifetime) + 0.20·z(rank_velocity) + 0.15·z(volume))
         × recency × trust
```

Missing components (no rank data, momentum null) → drop the term and **renormalize the remaining
weights**; record `weights_used` in `components`.

Once ≥14 days of own snapshots exist, add `acceleration := (v3 − v7) / max(v7, ε)` at weight 0.20
and scale the other weights by 0.8. That term separates decaying paid-UA spikes from compounding
organic growth.

**`trust` (0–1 multiplier):** start at 1.0; −0.4 if the developer released ≥5 apps within 14 days
(clone farm); −0.3 for a rating spike with no `version` change; −0.3 for relaunch signals
(`released` reset on a developer with history, or an iOS ratings reset per guard 6). Floor 0.1.
Hard-coded heuristics first; tune from stored components, not guesses.

**Always persist components** — every raw value, z-value, and weight. You will need to debug why
something ranked #1.

---

## 8. Workflows

```yaml
name: daily
on:
  schedule: [{ cron: '17 6 * * *' }]   # off the hour; Actions cron drifts
  workflow_dispatch:
concurrency: { group: daily, cancel-in-progress: false }
permissions: { actions: write }        # for the self-re-enable step
jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '22' }
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: npm ci && pip install -r requirements.txt
      - run: npx tsx src/jobs/discover.ts
      - run: npx tsx src/jobs/enrich.ts
      - run: npx tsx src/jobs/score.ts
      - name: keepalive             # resets GitHub's 60-day scheduled-workflow disable
        if: always()
        run: gh api -X PUT repos/${{ github.repository }}/actions/workflows/daily.yml/enable
        env: { GH_TOKEN: '${{ github.token }}' }
    env:
      SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
      SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
```

```yaml
name: backfill
on:
  schedule: [{ cron: '43 */6 * * *' }]
  workflow_dispatch:
concurrency: { group: backfill, cancel-in-progress: false }
# same setup steps; runs src/jobs/backfill.ts
```

**Backfill is time-boxed, not count-boxed:** process the `reviews_backfilled = false` queue
oldest-`first_seen`-first and stop cleanly at 40 minutes; the rest carries to the next run. At
Apple's ~20 req/min that's ~80 apps per run, ~320/day — comfortably above expected discovery
volume, without ever starving `daily`.

**Idempotency is mandatory** — Actions cron routinely fires hours late. Every job stamps
`snapshot.day` with the **UTC date it intends to cover**, so a late run still writes correct rows
and a re-run overwrites cleanly (`on conflict do update`).

Use a **public repo** — free standard-runner minutes. Consequence: the code and keyword lists are
public (fine), and the 60-day auto-disable applies (handled by the keepalive step).

---

## 9. Environment

```
SUPABASE_URL
SUPABASE_SERVICE_KEY      # server only, never NEXT_PUBLIC_
SUPABASE_ANON_KEY         # dashboard
DASHBOARD_PASSWORD        # simplest single-user auth
```

Dashboard auth: a single password in Vercel middleware is sufficient and takes ten minutes.
Supabase Auth magic links are the alternative if you want it on your phone without a password
manager.

---

## 10. Build order

| Phase | Ships | Done when |
|---|---|---|
| 0 | `spike/validate.ts` run from Actions | `VALIDATION.md` committed; Actions-vs-self-hosted decision made |
| 1 | Schema + Play pipeline (D1/D2/D4 discovery, enrich, snapshots) | A SQL query returns new Play apps ranked by exact daily installs |
| 2 | iOS pipeline (`newapplications` + charts + lookup snapshots + review backfill) | Same for iOS; empty-feed guard tested by deliberately hammering the endpoint |
| 3 | Scoring + `run_log` | Scores populate; components inspectable; obvious junk filtered |
| 4 | Dashboard | Sortable table; filters (store/category/days-since-release/discovered_via); velocity sparkline |
| 5 | Trust heuristics, twin matching, D3 similar-crawl, retention rollup | Clone farms suppressed; weekly rollup running |

Deferred: sitemap enumeration, trackId sweeping. Variation engine last, reading `breakout_score`.

---

## 11. Open questions (what Phase 0 must still answer)

1. **Do Actions runner IPs survive both stores, and at what request rate?** Decides
   Actions-vs-self-hosted. Everything else in §0 is already settled.
2. Young-app review distribution — how often does the 500-review cap truncate history? Decides how
   much weight backfilled momentum deserves on day one.
3. Search-sweep yield per keyword — sizes the D1 keyword list.
4. Do the `newfreeapplications` / `newpaidapplications` feed variants work too?
5. **The novel metric:** once twins are matched, correlate iOS rating velocity against Play exact
   install deltas. Nobody publishes the review-to-install ratio; this system can measure it.
   Prioritise once Phase 4 lands.
