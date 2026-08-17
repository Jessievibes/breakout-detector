# Phase 0 findings — evidence log and spec deltas

Everything below was measured live, not inferred from documentation. Dates matter: store
internals change without notice, and a claim without a date is a claim without a shelf life.

Runs: residential IP, 2026-08-12 (initial probes) and 2026-08-17 (full spike, all gates pass).
**Still unmeasured: the GitHub Actions runner IP.** That is the whole remaining point of Phase 0.

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

### 2.4 `dev?id=` is 404; `developer?id=<name>` works

The legacy developer URL is gone. The working form takes the developer **name**, not the numeric
id: `https://play.google.com/store/apps/developer?id=Spotify+AB`. No library support — a thin
regex over `/store/apps/details?id=([a-zA-Z0-9._]+)` is enough. Verified on 5 developers.

Schema consequence: `app.developer` (the name) is the D2 crawl key, not `developer_id`.

---

## 3. The real problem Phase 0 exposed: Play has no good new-app discovery

Play carries the single best signal in the system — exact daily installs — and the worst
discovery. Every channel measured:

| Channel | Status | Recall for young apps |
|---|---|---|
| `NEW_FREE` / `NEW_PAID` collections | **dead** — 200 with zero app links | none |
| Sitemaps | **useless** — no dates, mixed types (§2.2) | none |
| Cross-store from iOS | **17%** match rate (§2.3) | poor |
| Keyword search (D1) | works once wrapped, but ranks by relevance × popularity | **0 of 8 sampled apps were <90 days old** |
| Install-band pre-filter on search | free field, but low-band 0% vs high-band 0% young in the quick run | inconclusive — needs the full run's 12/stratum |
| Developer pages (D2) | **works** (§2.4) | high precision, but only for developers already in the DB |
| Category charts (D4) | works | late by construction — charting *is* the breakout |

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

## 5. What Phase 0 still has to answer

1. **Does the Actions runner IP survive both stores, and at what rate?** Everything above is
   residential. Run `.github/workflows/validate.yml` and diff against the baseline table in
   `VALIDATION.md`. If T1/T2 fail there while passing here, it is the IP, not the code.
2. Does the install-band pre-filter concentrate young apps? Needs the full run (12/stratum).
3. Truncation rate on a larger stratified young-app sample.
