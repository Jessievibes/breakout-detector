# Phase 0 findings — evidence log and spec deltas

Everything below was measured live, not inferred from documentation. Dates matter: store
internals change without notice, and a claim without a date is a claim without a shelf life.

Runs: residential IP 2026-08-12 and 2026-08-17; **GitHub Actions runner 2026-08-17** (run
32043548775, 12.4 min, all gates pass).

---

## 0. The Actions IP verdict — PASS

Phase 0 existed to answer one question: does a GitHub Actions runner IP survive both stores?

**It does.** 193 requests from the runner: zero 403s, zero 429s, zero 5xx, zero
empty-but-200 responses. Anomaly rate 0.0%, identical to the residential baseline. The
sustained-volume test (100 sequential Play detail fetches at 2–5 s jitter) returned 100 good
rows with a 0% parser-null rate.

Consequences:

- The deployment model in SPEC.md §2 stands. No self-hosted runner needed.
- Free public-repo minutes mean job duration genuinely doesn't matter.
- **This is a starting position, not a permanent one.** IP reputation degrades gradually
  rather than failing cleanly, which is exactly why `run_log` records `empty_200` / `http_403`
  per run and the fetcher aborts at a 10% anomaly rate. Phase 0 says where we start; that
  instrumentation says when it rots.

Two results sharpened by the full (non-quick) run:

| | Quick run | Full Actions run |
|---|---|---|
| Young-app review truncation (T4) | 25% of 4 | **10% of 10** — iOS backfill is effectively complete |
| Search sweep (T6) | 74 apps / 3 terms | **248 apps / 10 terms**, cap 30/term |

---

## 1. Confirmed good

| Fact | Evidence |
|---|---|
| Play `realInstalls` is exact | 12 packages spanning 470K → 3.09B installs, every one exact (≠ `minInstalls` band floor). OsmAnd 470,723; Bandcamp 4,961,931; Spotify 3,091,946,484 |
| Play detail is one cheap request | metadata + histogram + `released` + `ratings` + `reviews` in a single fetch via pinned `google-play-scraper==1.2.7` |
| Sustained Play volume is tolerated | 15 sequential detail fetches with 2–5 s jitter: 0 anomalies, 0 parser-nulls (residential) |
| Apple chart feeds | `topfreeapplications` returns exactly 100 per genre; `limit=200` still returns 100 |
| Apple new-app feeds — **all three variants work** | `newapplications`, `newfreeapplications`, `newpaidapplications` all return ~100/genre. (`newpaidapplications` returned 108 — cap is soft) |
| Apple lookup batching | 200 ids sent → 200 results |
| Apple reviews XML | 50 entries/page, 10 pages max |
| Apple reviews JSON | 50 entries — **the zero-entry bug did not reproduce.** Still use XML; JSON's first entry is the app itself, an easy off-by-one |
| Apple timestamps | carry local offsets (`2026-08-15T20:56:55-07:00`), parse cleanly to UTC. Normalize at ingest |
| Play reviews `batchexecute` | continuation tokens work; 100/page |
| Ratings-to-reviews ratio | Spotify 36,136,664 ratings / 1,847,752 reviews = **5.1%**. Confirms `RATINGS_PER_REVIEW ≈ 20` |

---

## 2. Confirmed dead or broken — each forces a spec change

### 2.1 `play_search` crashes on thin result sets — **fix before any sweep code ships**

`google-play-scraper==1.2.7`, `search.py:41`:

```python
top_result = dataset["ds:4"][0][1][0][23][16]   # unguarded index chain
```

Any query without a strong top result raises `TypeError: 'NoneType' object is not subscriptable`.
Reproduced on `BogeyBreaker`, `ICLinkWorld`, `zzqxwvunlikelyquery12345`. In the first cross-store
probe, **5 of 12 queries died this way**.

An unwrapped keyword sweep dies on its first obscure term. `spike/validate.py:safe_search()` is
the wrapper, and it graduates to `src/lib/` verbatim. Note the distinction it preserves: a genuine
thin result set returns `[]`, an unexpected failure returns `None`. Collapsing those two is how
you write "no such apps" as a fact.

### 2.2 Play sitemaps are useless for new-app discovery — un-park them permanently

Spec v2 parked sitemaps as "impractical but exhaustive". They are worse than impractical:

- **No `<lastmod>`, no `<changefreq>`** — nothing to filter or diff on except full-snapshot diffing
- **Mixed content types** — shard 00000 held 384 URLs, of which **47** were app details; the rest
  were books and container pages
- 82,937 shards per index snapshot, ~6.5 MB uncompressed each

Delete the "revisit if the system earns a bigger box" note. There is nothing to revisit.

### 2.3 Cross-store name matching is not a discovery channel — 17%

Hypothesis: Apple's working new-app feed could seed Play discovery by name. Measured: **2 of 12**
brand-new iOS Utilities apps found on Play by name search. Brand-new indie apps are mostly
single-store at launch.

Two consequences:
1. Play discovery gets no help from iOS. It stands on its own channels.
2. **Spec §11 Q5 needs tempering.** The novel review-to-install correlation depends on a matched
   twin population, and if only ~17% of young apps have twins, that population is thin and biased
   toward better-resourced developers. Still worth measuring — but as a study on a biased subsample,
   not a general constant.

### 2.4 Play serves developers from TWO URL forms, and picking one loses half of D2

Corrects an earlier, too-broad claim in this file that "`dev?id=` is 404". It is 404 *for
names*. It is the **correct and only** form for numeric developer ids. Verified by reading the
links Play's own detail pages emit:

| `developerId` | Working URL | Other form |
|---|---|---|
| numeric (`4949773854634494965`) | `dev?id=<numeric>` → 200 | `developer?id=<numeric>` → 404 |
| a name (`Spotify AB`) | `developer?id=<name>` → 200 | `dev?id=<name>` → 404 |

In an 8-app sample the split was 5 numeric / 3 name, so **using names alone silently loses
roughly half of channel D2** — silently because the wrong form returns a clean 404, which is
indistinguishable from "this developer has no apps".

Two traps inside the trap:

1. **Do not re-encode `developerId` for the name case.** The library returns it pre-encoded
   (`Spotify+AB`); running `quote_plus` over that yields `%2B` and 404s. Quote the raw
   `developer` name instead. Commas are fine either way (`Notion+Labs%2C+Inc.` → 200).
2. **A developer page returning nothing is usually a stale name, not an empty developer.**
   `developer_crawl` now tries both forms and reports the empty ones rather than shrugging.

Schema consequence: D2 needs **both** `app.developer` and `app.developer_id`, not just the name.

How this was found is worth recording: the first Actions run reported T7 at 3/5, which looked
like an IP or parser problem. It was neither — two fixtures were simply stale (Niantic had sold
Pokémon GO to Scopely Explore, Inc.; Todoist's developer is "Todoist Inc.", not "Doist").
Chasing a wrong-looking test result surfaced a real bug that the passing 3 had been hiding.

---

## 3. The real problem Phase 0 exposed: Play has no good new-app discovery

Play carries the single best signal in the system — exact daily installs — and the worst
discovery. Every channel measured:

| Channel | Status | Recall for young apps |
|---|---|---|
| `NEW_FREE` / `NEW_PAID` collections | **dead** — 200 with zero app links | none |
| Sitemaps | **useless** — no dates, mixed types (§2.2) | none |
| Cross-store from iOS | **17%** match rate (§2.3) | poor |
| Keyword search (D1) | works once wrapped, but ranks by relevance × popularity | **0 of 24 sampled apps were <90 days old** |
| Install-band pre-filter on search | **dead idea** — low-band 0% vs high-band 0% young across 24 stratified samples | no better than random |
| Developer pages (D2) | **works, via both URL forms** (§2.4) | high precision, but only for developers already in the DB |
| Category charts (D4) | works — `/store/apps/top` ~95 ids, category pages 43–62 | late by construction — charting *is* the breakout |

**D1's real job is not finding young apps.** The full Actions run settled this: 248 distinct
apps from 10 keywords, and *zero* of 24 stratified samples were under 90 days old. Install-band
pre-filtering, the one idea that might have rescued it, performed exactly as well as random.

So search should be understood as a **seed generator for D2**, not as a discovery channel in
its own right. Every app it surfaces contributes a developer, and developers are where new apps
actually appear — a real example from testing: crawling "Notion Labs, Inc." surfaces
`com.cron.calendar`, an acquisition no keyword would ever connect to Notion. Judge D1 on
developers-per-request, not apps-per-request.

**Honest architectural consequence:** the two store pipelines are not symmetric, and the spec
should stop implying they are.

- **iOS = day-zero discovery, coarser velocity.** Three working new-app feeds, ~100/genre across
  ~25 genres. Apps are catchable the day they ship. Velocity comes from `Δ userRatingCount`.
- **Play = lagging discovery, exact velocity.** Apps are typically found *after* some traction
  (via D2 seeds or charts), but once found, `Δ realInstalls` is the truest signal available.

This is complementary rather than fatal, and it sharpens what the dashboard means: an iOS row can
be a genuine day-3 catch, while a Play row is usually "already moving, now measured precisely."
But it does mean **Play will systematically miss the earliest window**, and no amount of scraping
fixes that — the discovery surface simply is not exposed any more.

Bootstrapping consequence for Phase 1: Play discovery is seed-dependent (D2 needs developers,
D3 needs high scorers), so the cold-start pool must come from D1 + D4, and the D1 keyword list
has to be large. Track `median(first_seen − released)` per channel from the first run; it is the
metric that tells you whether Play discovery is worth its request budget at all.

---

## 4. Methodology correction worth keeping

The first T4 run reported **75%** of young apps truncated at the 500-review cap. That was an
artifact: it probed the 4 *highest-rated* young apps, i.e. the worst case by construction.
Stratifying across the rating distribution gave **25%**.

The finding stands directionally — popular young apps do lose history to the cap — but the
pipeline consequence is milder than the first number implied, and it independently validates spec
v2's switch of iOS velocity from text-review arrival to `Δ userRatingCount`: ratings are ~20×
denser and never truncated.

Generalize the lesson: any measurement that picks extremes measures extremes. Stratify.

---

## 5. Phase 0 questions — all answered

1. ~~Does the Actions runner IP survive both stores?~~ **Yes** — 193 requests, 0% anomalous (§0).
2. ~~Does the install-band pre-filter concentrate young apps?~~ **No** — no better than random (§3).
3. ~~Truncation rate on a stratified young-app sample?~~ **10%** — iOS backfill effectively complete.

Phase 0 is closed. What it leaves open is not a question but a standing risk: IP reputation
decays, so `run_log`'s anomaly counters are the live continuation of this work.

The open question that now matters most is a Phase 1 one: **does Play discovery find apps early
enough to be worth its request budget?** Track `median(first_seen − released)` per channel from
the first real run. If D1 and D4 only ever surface apps that are already months old and D2 is
the only channel with real lead time, the request budget should shift accordingly.
